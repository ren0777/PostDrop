"""Caption AI: captions, tags and alt text written by a vision model.

The provider is OpenCode Go or Google Gemini, both spoken to in the OpenAI
Chat Completions format (see PROVIDERS). The primary model looks at the image; a backup model takes over when it fails, and a
name-only call is the last resort (for example when a model refuses a
suggestive image).

Only the Chat Completions shape is implemented. Qwen and MiniMax use the
Anthropic Messages format: an adapter can be added next to `_call` later
without touching the rest of this module.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from io import BytesIO

import httpx
from PIL import Image

from .images import prepare
from .post import clip_title, parse_tags

log = logging.getLogger("artdrop.caption")

ENDPOINT = "https://opencode.ai/zen/go/v1/chat/completions"
VISION_TIMEOUT = 120.0  # a caption call may look at a large cover
IDENTIFY_TIMEOUT = 60.0  # "who is in this picture?" is small and quick
NAME_ONLY_TIMEOUT = 30.0
MAX_IMAGE_BYTES = 1_000_000
MAX_IMAGE_SIDE = 1024
# Recognising a character (and the alt-text peeks of a mixed post) needs less:
# small images make the calls fast and stop them timing out on big sets.
SMALL_IMAGE_BYTES = 300_000
SMALL_IMAGE_SIDE = 512
MIXED_IMAGE_LIMIT = 4  # cover + three spread across the set

DEFAULT_VISION_MODEL = "deepseek-v4.1-flash"
DEFAULT_BACKUP_MODEL = "mimo-v2.6-flash"

# Both providers speak Chat Completions; only the address, the key and the
# default models differ. Gemini's OpenAI-compatible endpoint takes a key from
# aistudio.google.com as a Bearer token.
PROVIDERS = {
    "opencode": {
        "label": "OpenCode Go",
        "endpoint": ENDPOINT,
        "vision_model": DEFAULT_VISION_MODEL,
        "backup_model": DEFAULT_BACKUP_MODEL,
    },
    "gemini": {
        "label": "Google Gemini",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        # Flash-Lite answers in seconds; 3.8 Flash was overloaded or over the
        # free quota when tried, so the slower 3.6 Flash is the backup.
        "vision_model": "gemini-3.5-flash-lite",
        "backup_model": "gemini-3.6-flash",
    },
}
DEFAULT_PROVIDER = "opencode"
# The settings each provider keeps for itself, so switching back loses nothing.
PROFILE_KEYS = ("api_key", "vision_model", "backup_model", "strong_model")


def provider(cfg: dict) -> str:
    name = str(cfg.get("provider") or DEFAULT_PROVIDER)
    return name if name in PROVIDERS else DEFAULT_PROVIDER


def provider_label(cfg: dict) -> str:
    return PROVIDERS[provider(cfg)]["label"]


def vision_model(cfg: dict) -> str:
    return str(cfg.get("vision_model") or PROVIDERS[provider(cfg)]["vision_model"])


def backup_model(cfg: dict) -> str:
    return str(cfg.get("backup_model") or PROVIDERS[provider(cfg)]["backup_model"])


def switch_provider(cfg: dict, name: str) -> dict:
    """The settings with `name` active: the old provider's key and models are put
    aside under "profiles" and the new one's are brought back."""
    if name not in PROVIDERS or name == provider(cfg):
        return cfg
    cfg = dict(cfg)
    profiles = dict(cfg.get("profiles") or {})
    profiles[provider(cfg)] = {key: cfg[key] for key in PROFILE_KEYS if cfg.get(key)}
    for key in PROFILE_KEYS:
        cfg.pop(key, None)
    cfg.update(profiles.pop(name, {}))
    cfg["profiles"] = profiles
    cfg["provider"] = name
    return cfg


def _require_key(cfg: dict) -> None:
    if not str(cfg.get("api_key") or "").strip():
        raise CaptionError(
            f"Caption AI is not set up yet — add your {provider_label(cfg)} API key.")

# Models sometimes end the caption with the hashtag line themselves.
_WHOLE_TAG_LINE_RE = re.compile(r"^\s*(?:#[\w.]+(?:\s+|$))+$", re.UNICODE)
_TRAILING_TAGS_RE = re.compile(r"(?:^|\s)(#[\w.]+(?:\s+#[\w.]+)*)\s*$", re.UNICODE)
_TAG_WORD_RE = re.compile(r"#([\w.]+)", re.UNICODE)

_STATUS_HINTS = {
    401: "API key rejected",
    403: "API key rejected",
    404: "model not available (retired or renamed) — pick another model in Accounts → Caption AI",
    410: "model not available (retired or renamed) — pick another model in Accounts → Caption AI",
    429: "rate limit reached",
}

# OpenCode Go asks clients to identify themselves and send a session id.
USER_AGENT = "ArtDrop/1.0"
_SESSION_ID = uuid.uuid4().hex

# Style rules per site. Styles live here so later connectors can add their own:
# DeviantArt (up to 30 tags, no #), Instagram (10-15 hashtags), Tumblr (tags as
# a separate field), Reddit (a title only, no hashtags), Pixiv (Japanese and
# English tags). `limit` mirrors the connector's caption_limit — a test keeps
# the two in step.
SITE_RULES = {
    "bluesky": {
        "caption": "one or two short lines",
        "tags": "3-5 short tags",
        "limit": 300,
    },
    "telegram": {
        "caption": "2-4 lines; warmer and more descriptive",
        "tags": "6-10 tags",
        "limit": 1024,
    },
    "discord": {
        "caption": "casual, one line",
        "tags": "0-3 tags",
        "limit": 2000,
    },
    "instagram": {
        "caption": "1-3 short lines, friendly and upbeat",
        "tags": "10-15 hashtags (at most 30)",
        "limit": 2200,
    },
    "tumblr": {
        "caption": "1-3 lines",
        "tags": "5-15 tags; they go in Tumblr's own tag field, not in the text",
        "limit": 4096,
        "tags_in_text": False,
    },
    "deviantart": {
        "caption": "the description can be 2-4 lines",
        "tags": "up to 30 tags using letters, numbers and underscore only",
        "limit": 65_535,
        "tags_in_text": False,
    },
    "pixiv": {
        "caption": "1-3 short lines, in Japanese",
        "tags": ("at most 10 tags, in Japanese, in this order: (1) the series' official Japanese "
                 "title when you are certain of it (for example 五等分の花嫁), (2) the character's "
                 "official Japanese full name when you are certain of it (for example 中野二乃), "
                 "otherwise the name exactly as the artist typed it, (3) then descriptive tags. "
                 "Never invent or guess a Japanese name"),
        "limit": 4096,
        "tags_in_text": False,
        "title_limit": 32,
        "title_hint": "Japanese",
        # Pixiv flags AI works with its own setting, so an AI tag here only
        # wastes one of the ten slots (see docs/ARCHITECTURE.md, Pixiv rules).
        "no_ai_tags": True,
        # The artist's own spelling of the character is always one of the tags.
        "character_tag": True,
        "max_tags": 10,
    },
}

# Spellings of an AI-disclosure tag. Sites with their own AI flag (Pixiv) get
# these removed, because a tag costs one of their ten slots.
AI_TAGS = frozenset({
    "ai", "aiart", "aiartwork", "aiartworks", "aiillustration", "aiillustrations",
    "aigenerated", "aigeneratedart", "aidrawing", "aidrawings", "aipicture",
    "aipictures", "aipaint", "aipainting", "aicomics", "aimanga",
    "aiアート", "aiイラスト", "aiイラストレーション", "ai生成", "ai生成作品", "ai絵",
})


class CaptionError(Exception):
    """A failure with a message that is safe to show the user."""


class CaptionUnavailable(CaptionError):
    """The model was unreachable or too slow — worth another pass later.

    Raised for timeouts, connection errors, 429 and 5xx. The lazy worker keeps
    the work unprocessed and retries with a backoff instead of sending it to
    "Needs you" straight away.
    """


def _is_transient(status: int) -> bool:
    return status >= 500 or status in (408, 429)


def parts_rules(parts: dict | None, platforms, *, exact: bool = False) -> list[str]:
    """Sites that split the set into several posts get one caption per post."""
    if exact:
        return []  # the artist's words are used on every post
    lines = []
    for site in platforms:
        count = int((parts or {}).get(site) or 1)
        if count > 1:
            lines.append(
                f'- {site} splits this set into {count} separate posts: inside its entry also '
                f'give "captions", a list of {count} captions, one per post. Each must be '
                "worded differently (no two may open the same way) while following the same "
                'site style; the first one is the same as "caption".')
    return lines


def with_parts(entry: dict, raw: dict) -> dict:
    """Keep a site's per-post captions, cleaned like its main caption."""
    given = raw.get("captions")
    captions = []
    for text in given if isinstance(given, list) else []:
        cleaned, _ = _strip_trailing_tags(str(text or ""))
        if cleaned:
            captions.append(cleaned)
    if captions:
        entry["captions"] = captions
    return entry


def build_lazy_prompt(*, platforms, mature: bool = False, has_image: bool = True,
                      add_ai_tag: bool = True, caption_draft: str = "",
                      caption_exact: bool = False, given: dict | None = None,
                      parts: dict | None = None) -> str:
    """The lazy-mode prompt: no character was typed, so identify it first."""
    base = build_prompt(character="(recognise it from the image)", series="", notes="",
                        mature=mature, platforms=platforms, has_image=has_image,
                        add_ai_tag=add_ai_tag)
    lines = base.split("\n")
    rules = [
        "- The artist did not name the character: recognise the character and the series from the "
        "image, and add these two keys to the JSON:",
        '  "character": the character\'s name, as it is normally written in English (e.g. "Nino '
        'Nakano"); "" when you cannot tell.',
        '  "series": the series or game it is from (e.g. "The Quintessential Quintuplets"); "" '
        "when you cannot tell.",
        '- "confident": true only when you are sure of both the character and the series; false '
        "otherwise. Do not guess: a wrong name is worse than an empty one.",
    ]
    rules += caption_rules(draft=caption_draft, exact=caption_exact)
    rules += parts_rules(parts, platforms, exact=bool(caption_draft and caption_exact))
    block = given_block(given)
    if block:
        rules += block.split("\n")
    index = lines.index("- Use exactly these site keys: " + ", ".join(platforms))
    lines[index:index] = rules
    return "\n".join(lines)


async def generate_lazy(*, cfg: dict, images, platforms=(), mature: bool = False,
                        caption_draft: str = "", caption_exact: bool = False,
                        given: dict | None = None, parts: dict | None = None) -> dict:
    """Lazy mode: one call per batch that identifies the character and captions it.

    The work is split in two halves — the Western sites and Pixiv (Japanese) — and
    they run in parallel, so one slow half cannot fail the other.
    """
    _require_key(cfg)
    platforms = list(platforms or [])
    if not platforms:
        raise CaptionError("Lazy mode has no sites to post to.")
    image = _image_part(list(images)[0]) if images else None
    if image is None:
        raise CaptionError("Lazy mode needs the artwork to look at.")
    halves = _halves(platforms)
    if len(halves) == 1:
        result = await _lazy_call(cfg, image, platforms=halves[0], mature=mature,
                                  caption_draft=caption_draft, caption_exact=caption_exact,
                                  given=given, parts=parts)
        return {**result, "seen": [result.get("character") or ""]}
    results = await asyncio.gather(
        *(_lazy_call(cfg, image, platforms=half, mature=mature,
                     caption_draft=caption_draft, caption_exact=caption_exact, given=given,
                     parts=parts)
          for half in halves),
        return_exceptions=True,
    )
    merged = _merge_halves(results, platforms)
    # who each half thought it was: the Pixiv half can disagree with the Western one
    merged["seen"] = [str(result.get("character") or "") for result in results
                      if isinstance(result, dict)]
    return merged


async def _lazy_call(cfg: dict, image, *, platforms: list[str], mature: bool,
                     caption_draft: str, caption_exact: bool,
                     given: dict | None = None, parts: dict | None = None) -> dict:
    prompt = build_lazy_prompt(platforms=platforms, mature=mature, has_image=True,
                               caption_draft=caption_draft, caption_exact=caption_exact,
                               given=given, parts=parts,
                               add_ai_tag=bool(cfg.get("add_ai_tag", True)))
    models = [(vision_model(cfg), True),
              (backup_model(cfg), True)]
    last_error = ""
    errors: list[Exception] = []
    for attempt, (model, with_image) in enumerate(models, start=1):
        try:
            raw = await _attempt(cfg, model,
                                 _messages(prompt, image if with_image else None),
                                 VISION_TIMEOUT, attempt)
            data = parse_response(raw)
        except Exception as exc:  # noqa: BLE001 — try the next attempt
            errors.append(exc)
            last_error = (str(exc) if isinstance(exc, CaptionError)
                          else f"{type(exc).__name__}: {exc}")
            continue
        character = str(data.get("character") or "").strip()
        result = normalize(data, platforms, character=character)
        if caption_draft and caption_exact:  # word for word, whatever the model answered
            result = dict(result)
            for entry in (result.get("platforms") or {}).values():
                if isinstance(entry, dict):
                    entry["caption"] = str(caption_draft).strip()
                    entry.pop("captions", None)
        for site, entry in (result.get("platforms") or {}).items():
            if isinstance(entry, dict):
                entry["tags"] = ensure_tags(entry.get("tags") or [],
                                            required_tags(given, site), site)
        result["character"] = character
        result["confident"] = _as_bool(data.get("confident")) and bool(character)
        result["used_model"] = model
        return result
    raise _final_error(last_error, errors)


def build_identify_prompt() -> str:
    """The short prompt behind auto-split: who is in this picture?

    Anime/game fan art: the model is told to answer only when it would bet on it,
    to note the visual cues it used (internal), and to say when the picture looks
    like an original character (those go to "Needs you", never into a guessed set).
    """
    lines = [
        "You name the characters in one anime-style illustration. Most of the art is "
        "fan art from anime and gacha games (Zenless Zone Zero, Honkai: Star Rail, "
        "Genshin Impact, Wuthering Waves and similar).",
        "Reply with ONLY a JSON object, no markdown fences, in this shape:",
        '{"characters": ["Stelle"], "series": "Honkai: Star Rail", "confident": true, '
        '"cues": "green eyes, white coat, bat", "original": false}',
        "- characters: the characters' names as they are normally written in English; use "
        "several names for a duo or group piece; an empty list when you cannot tell.",
        '- series: the series or game they are from, or "" when you cannot tell.',
        "- confident: true only when you would bet on the name — a newer character you are "
        "unsure about is not confident. Never guess: a wrong name is worse than an empty one.",
        "- cues: a few words about what in the picture led you to the name (kept for "
        "debugging, never posted).",
        '- original: true when the picture looks like an original character of the '
        'artist\'s own rather than a known one (then leave characters empty).',
    ]
    return "\n".join(lines)


def _answer(data: dict, model: str) -> dict:
    """One model's identify answer, normalised."""
    names = [str(name).strip() for name in (data.get("characters") or [])
             if str(name).strip()]
    if not names and str(data.get("character") or "").strip():
        names = [str(data["character"]).strip()]  # models sometimes simplify the key
    confident = _as_bool(data.get("confident")) and bool(names)
    if _as_bool(data.get("original")):
        confident = False  # an original character is for the artist to caption
    return {
        "characters": names[:8],
        "series": str(data.get("series") or "").strip(),
        "confident": confident,
        "original": _as_bool(data.get("original")),
        "cues": str(data.get("cues") or "").strip()[:200],
        "used_model": model,
    }


def _same_cast(one: dict, other: dict) -> bool:
    """True when two answers name the same characters (order and case aside)."""
    left = sorted(str(name).casefold() for name in one.get("characters") or [])
    right = sorted(str(name).casefold() for name in other.get("characters") or [])
    return left == right


async def identify(*, cfg: dict, image, verify: bool = True, escalate: bool = True) -> dict:
    """One image in, who is in it out — small and cheap, for M6.1 auto-split.

    The chain is primary → backup → the optional `strong_model`; the first
    confident answer wins. With a strong model configured it also cross-checks a
    confident answer, and two models naming different characters mean "not
    confident" (a wrong name is worse than none).
    """
    _require_key(cfg)
    image_part = _image_part(image, max_bytes=SMALL_IMAGE_BYTES, max_side=SMALL_IMAGE_SIDE)
    prompt = build_identify_prompt()
    vision = vision_model(cfg)
    backup = backup_model(cfg)
    strong = str(cfg.get("strong_model") or "").strip()
    models = [vision]
    if escalate:  # the per-pass escalation budget is spent else: the primary only
        models += [model for model in (backup, strong) if model and model not in (vision,)]
    last_error = ""
    errors: list[Exception] = []
    confident: dict | None = None
    unsure: dict | None = None
    for attempt, model in enumerate(models, start=1):
        try:
            raw = await _attempt(cfg, model, _messages(prompt, image_part),
                                 IDENTIFY_TIMEOUT, attempt)
            answer = _answer(parse_response(raw), model)
        except Exception as exc:  # noqa: BLE001 — try the next model
            errors.append(exc)
            last_error = (str(exc) if isinstance(exc, CaptionError)
                          else f"{type(exc).__name__}: {exc}")
            continue
        if not answer["confident"]:
            unsure = answer  # an answer, just not a confident one
            continue  # low confidence moves on to the next model
        if confident is None:
            confident = answer
            if not (strong and verify and model != strong):
                return confident  # the first confident answer, unless a check is wanted
            continue  # ask the strong model to confirm
        if not _same_cast(confident, answer):
            log.info("identify: %s and %s disagree (%s vs %s) — not confident",
                     confident["used_model"], model, confident["characters"],
                     answer["characters"])
            return {**confident, "confident": False,
                    "disagreement": f"{confident['used_model']} vs {model}"}
        return confident  # both agree
    if confident is not None:
        return confident  # the verifier was unsure or failed: keep the answer
    if unsure is not None:
        return unsure  # the models answered; they are simply not sure who it is
    if not escalate and not errors:
        # the primary was unsure and escalation is out of budget: try next pass
        return {"characters": [], "series": "", "confident": False,
                "escalate_skipped": True, "used_model": vision}
    raise _final_error(last_error, errors)


def _final_error(last_error: str, errors: list[Exception]) -> CaptionError:
    """Every attempt failed: transient causes stay retryable, the rest do not."""
    message = f"Caption AI failed — {last_error or 'no model answered'}"
    if errors and all(isinstance(exc, CaptionUnavailable) for exc in errors):
        return CaptionUnavailable(message)
    return CaptionError(message)


# Tag limits per site, mirroring the connectors (a test keeps them in step).
TAG_LIMITS = {"pixiv": 10, "instagram": 30, "deviantart": 30}

def given_block(given: dict | None) -> str:
    """The rules for a set whose character the artist's folder name already gives."""
    if not given or not str(given.get("name") or "").strip():
        return ""
    name = str(given["name"]).strip()
    series = str(given.get("series") or "").strip()
    if given.get("source") == "identify":
        lines = [f"- The character is \"{name}\" (identified beforehand, a fact): use that "
                 "name and put it in the \"character\" key."]
    else:
        lines = [
            f"- The character is exactly \"{name}\" (the artist's own folder name): use that "
            "name as written and put it in the \"character\" key. Do not replace it with "
            "another character, and if you do not recognise it, keep the name as it is.",
        ]
    lines.append("- Do not name any other character anywhere: not in the captions, the tags "
                 "or the alt text.")
    if series:
        lines.append(f"- The series is exactly \"{series}\" (a fact): use it as the \"series\".")
    else:
        lines.append("- Fill in the series or game only when you are sure of it.")
    lines.append("- On Pixiv, use the character's official Japanese name only when you are "
                 "sure of it; otherwise use the name as written.")
    return "\n".join(lines)


def _name_key(value: str) -> str:
    """A name for comparing: lowercase letters and digits only ("Hu Tao" = "hutao")."""
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def same_name(one: str, other: str) -> bool:
    """True for the same character, allowing a short or long form (Raiden / Raiden Shogun)."""
    left, right = _name_key(one), _name_key(other)
    if not left or not right:
        return False
    return left == right or (min(len(left), len(right)) >= 3
                             and (left in right or right in left))


def _split_names(value: str) -> list[str]:
    parts = re.split(r",|&|/|\band\b", str(value or ""))
    return [part.strip() for part in parts if part.strip()]


def character_mismatch(result: dict, names, others=()) -> str:
    """Why a caption result disagrees with the set's character(s), or "" when it agrees.

    Two cheap checks, no extra AI call: every half of the call must report one
    of the set's names as its "character" (an empty answer is not a
    disagreement), and no Western caption or alt text may name another known
    character. Pixiv's Japanese text is covered by the first check only.
    """
    names = [str(name).strip() for name in (names or []) if str(name).strip()]
    if not names:
        return ""
    seen = result.get("seen")
    if not isinstance(seen, list):
        seen = [result.get("character")]
    for reported in seen:
        for part in _split_names(reported):
            if not any(same_name(part, name) for name in names):
                return f"the AI called it \"{part}\", not {' / '.join(names)}"
    texts = [str(entry.get("caption") or "")
             for site, entry in (result.get("platforms") or {}).items()
             if site != "pixiv" and isinstance(entry, dict)]
    texts.append(str(result.get("alt_text") or ""))
    for other in others or ():
        other = str(other or "").strip()
        if len(_name_key(other)) < 3 or any(same_name(other, name) for name in names):
            continue
        pattern = re.compile(r"(?<!\w)" + re.escape(other) + r"(?!\w)", re.IGNORECASE)
        if any(pattern.search(text) for text in texts):
            return f"the caption names \"{other}\", not {' / '.join(names)}"
    return ""


def required_tags(given: dict | None, site: str) -> list[str]:
    """The tags a set must carry: the Japanese name on Pixiv, the given one elsewhere."""
    if not given:
        return []
    name = str(given.get("name") or "").strip()
    japanese = str(given.get("japanese") or "").strip()
    if not name and not japanese:
        return []
    required = japanese if (site == "pixiv" and japanese) else (name or japanese)
    series = str(given.get("series") or "").strip()
    return [tag for tag in (required, series) if tag]


def _halves(platforms: list[str]) -> list[list[str]]:
    """The caption call split: the Western sites, then Pixiv (long, Japanese)."""
    western = [site for site in platforms if site != "pixiv"]
    halves = []
    if western:
        halves.append(western)
    if "pixiv" in platforms:
        halves.append(["pixiv"])
    return halves or [list(platforms)]


def _merge_halves(results, platforms) -> dict:
    """Both halves in parallel: keep what answered, only fail if nothing did."""
    merged: dict | None = None
    errors: list[BaseException] = []
    for result in results:
        if isinstance(result, BaseException):
            errors.append(result)
            continue
        if merged is None:
            merged = dict(result)
            continue
        merged["platforms"] = {**(merged.get("platforms") or {}),
                               **(result.get("platforms") or {})}
        if not merged.get("alt_text"):
            merged["alt_text"] = result.get("alt_text") or ""
    if merged is not None:
        for error in errors:
            log.warning("caption half failed (the other half still landed): %s", error)
        return merged
    last = str(errors[-1]) if errors else "no model answered"
    raise _final_error(last, [error for error in errors if isinstance(error, Exception)])


def tag_name(value: str) -> str:
    """A name as one tag: no spaces, no punctuation, case kept (HonkaiStarRail)."""
    return re.sub(r"[\s:：_\-–—!?.,'\"]+", "", str(value or "").strip())


def ensure_tags(tags, required, site: str = "") -> list[str]:
    """Every character and series must be tagged: generic tags make room first."""
    tags = [str(tag) for tag in (tags or [])]
    have = {_normalized_tag(tag) for tag in tags}
    missing = [name for name in (tag_name(item) for item in (required or []))
               if name and _normalized_tag(name) not in have]
    if not missing:
        return tags
    limit = TAG_LIMITS.get(site)
    if limit is None:
        return [*missing, *tags]
    return [*missing, *tags[:max(0, limit - len(missing))]]


def build_mixed_prompt(*, characters, series=(), platforms, mature: bool = False,
                       caption_draft: str = "", caption_exact: bool = False,
                       has_images: bool = True, given: dict | None = None,
                       parts: dict | None = None) -> str:
    """The prompt for a post that shows several characters at once (docs/ARCHITECTURE.md)."""
    lines = [
        "You write the caption for one social-media post that shows several characters at once.",
        "The pieces in this post are of different characters, so the caption must fit all of them.",
    ]
    if characters:
        lines.append("Characters in this post: " + ", ".join(characters))
    else:
        lines.append("The characters could not be named with confidence: write the caption "
                     "without naming any of them.")
    if series:
        lines.append("Series or games: " + ", ".join(series))
    lines += [
        "",
        "Rules:",
        "- Reply with ONLY a JSON object, no markdown fences, in this shape:",
        '{"title": "...", "alt_text": "...", '
        '"platforms": {"<site>": {"caption": "...", "tags": ["..."]}}}',
        "- The caption must not describe one character, one pose or one scene: it is a universal, "
        "warm, engagement-style line that fits every piece in the post.",
        '- Keep it short and playful, in the tone of: "Good morning ☀️", "Hello!", '
        '"Cute waifus today 💕", "Which one is your fav? Comment your fav below! 💬".',
        "- Vary the wording: never open the same way twice, and never reuse these examples.",
        "- Tags: include every character and every game/series listed above (one tag each, no spaces, "
        "e.g. Stelle, HonkaiStarRail), then a few generic art tags such as animeart, animegirl, fanart "
        "or digitalart. Stay inside each site's tag limit; when they do not all fit, drop generic tags "
        "first and never a character or a series. When no character could be named, use generic art "
        "tags plus any series you are sure of.",
        "- Captions must not contain hashtags: put every tag in the tags array instead.",
        "- Never invent facts about a character: no made-up relationships, ages, powers or quotes.",
        "- Captions and alt text must never mention AI, AI-generated, generated, illustrations, prompts "
        "or tools, and never use the words mature, tasteful, suggestive or explicit: AI disclosure is "
        "handled by tags and site flags, never by the caption text.",
    ]
    block = given_block(given)
    if block:
        lines += block.split("\n")
    lines += parts_rules(parts, platforms, exact=bool(caption_draft and caption_exact))
    if caption_draft:
        lines += caption_rules(draft=caption_draft, exact=caption_exact)
    else:
        lines.append('- "title": only when a site asks for one, in the language it asks for.')
    if has_images:
        lines.append("- alt_text: one short plain sentence for each attached image, joined with a "
                     "space, for screen readers.")
    else:
        lines.append("- alt_text: leave as an empty string (there is nothing to describe).")
    lines += ["", "Per-site style:"]
    for platform_id in platforms:
        rule = SITE_RULES.get(platform_id)
        if not rule:
            continue
        if rule.get("tags_in_text", True):
            ending = (f"the whole post (caption, blank line and tags) must fit "
                      f"{rule['limit']} characters.")
        else:
            ending = f"keep the caption under {rule['limit']} characters."
        line = f"- {platform_id}: {rule['caption']}; {rule['tags']}; {ending}"
        if rule.get("title_limit"):
            line += (f" It also needs its own title, in {rule.get('title_hint', 'English')}, "
                     f"at most {rule['title_limit']} characters, universal like the caption "
                     "(for example 推しはどの子？).")
        if rule.get("no_ai_tags"):
            line += (" Never add an AI-related tag here (no aiart, AIアート, AIイラスト, AI生成…): "
                     "this site labels AI works itself.")
        lines.append(line)
    if mature:
        lines += [
            "",
            "The artwork is suggestive or mature: keep every word tasteful and non-explicit, and do "
            "not describe anatomy or anything explicit.",
        ]
    else:
        lines += ["", "The artwork is safe for work."]
    return "\n".join(lines)


MIXED_FALLBACK = (
    "Which one is your fav? Tell me below! 💬",
    "A little of everyone today 💕 Who's your pick?",
    "Pick your favourite and drop it in the comments ✨",
)
MIXED_FALLBACK_JA = (
    "推しはどの子？💕 コメントで教えてね！",
    "今日はどの子が好み？💬 お気に入りを教えてね✨",
    "みんなまとめてお届け✨ 推しはどの子？",
)
MIXED_FALLBACK_TITLE_JA = "推しはどの子？"


def fallback_mixed_caption(site: str, characters=()) -> str:
    """A universal line for when the model left a mixed caption blank (varies per set)."""
    lines = MIXED_FALLBACK_JA if site == "pixiv" else MIXED_FALLBACK
    seed = sum(ord(char) for char in "".join(characters or ())) + len(site)
    return lines[seed % len(lines)]


async def generate_mixed(*, cfg: dict, images, characters=(), series=(), platforms=(),
                         mature: bool = False, caption_draft: str = "",
                         caption_exact: bool = False, given: dict | None = None,
                         parts: dict | None = None) -> dict:
    """A universal caption for a mixed set, with every character and series tagged.

    Split in two halves (Western sites and Pixiv) exactly like `generate_lazy`.
    """
    _require_key(cfg)
    platforms = list(platforms or [])
    if not platforms:
        raise CaptionError("Lazy mode has no sites to post to.")
    characters = [str(name).strip() for name in characters if str(name).strip()]
    series = [str(name).strip() for name in series if str(name).strip()]
    picked = _spread(list(images), MIXED_IMAGE_LIMIT)
    attachments = [_image_part(path, max_bytes=SMALL_IMAGE_BYTES, max_side=SMALL_IMAGE_SIDE)
                   for path in picked]
    if not attachments:
        raise CaptionError("Lazy mode needs the artwork to look at.")
    halves = _halves(platforms)
    if len(halves) == 1:
        return await _mixed_call(cfg, attachments, characters=characters, series=series,
                                 platforms=halves[0], mature=mature,
                                 caption_draft=caption_draft, caption_exact=caption_exact,
                                 given=given, parts=parts)
    results = await asyncio.gather(
        *(_mixed_call(cfg, attachments, characters=characters, series=series,
                      platforms=half, mature=mature, caption_draft=caption_draft,
                      caption_exact=caption_exact, given=given, parts=parts)
          for half in halves),
        return_exceptions=True,
    )
    return _merge_halves(results, platforms)


async def _mixed_call(cfg: dict, attachments: list, *, characters: list[str],
                      series: list[str], platforms: list[str], mature: bool,
                      caption_draft: str, caption_exact: bool,
                      given: dict | None = None, parts: dict | None = None) -> dict:
    prompt = build_mixed_prompt(characters=characters, series=series, platforms=platforms,
                                mature=mature, caption_draft=caption_draft,
                                caption_exact=caption_exact, has_images=True,
                                given=given, parts=parts)
    last_error = ""
    errors: list[Exception] = []
    for attempt, model in enumerate((vision_model(cfg),
                                     backup_model(cfg)),
                                    start=1):
        try:
            raw = await _attempt(cfg, model, _messages(prompt, attachments),
                                 VISION_TIMEOUT, attempt)
            data = parse_response(raw)
        except Exception as exc:  # noqa: BLE001 — try the next model
            errors.append(exc)
            last_error = (str(exc) if isinstance(exc, CaptionError)
                          else f"{type(exc).__name__}: {exc}")
            continue
        result = normalize(data, platforms)
        required = [*characters, *series]
        answers = result.setdefault("platforms", {})
        for site in platforms:  # a site the model skipped still gets its entry
            if not isinstance(answers.get(site), dict):
                answers[site] = {"caption": "", "tags": []}
        for site, entry in answers.items():
            if isinstance(entry, dict):
                must = required_tags(given, site) or required
                entry["tags"] = ensure_tags(entry.get("tags") or [], must, site)
                if caption_draft and caption_exact:
                    entry["caption"] = str(caption_draft).strip()
                    entry.pop("captions", None)
                elif not str(entry.get("caption") or "").strip():
                    # a blank answer (it happened live on Pixiv) never reaches a post
                    entry["caption"] = (str(caption_draft).strip()
                                        or fallback_mixed_caption(site, characters))
                    if site == "pixiv" and not str(entry.get("title") or "").strip():
                        entry["title"] = MIXED_FALLBACK_TITLE_JA
        result["character"] = ", ".join(characters)
        result["series"] = ", ".join(series)
        result["confident"] = True
        result["mixed"] = True
        result["used_model"] = model
        return result
    raise _final_error(last_error, errors)


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in ("true", "yes", "1")


def caption_rules(*, draft: str = "", exact: bool = False) -> list[str]:
    """The two ways the artist's own text is used (docs/ARCHITECTURE.md, caption.txt).

    Polish (default): the text is a draft the model rewrites per site.
    Exact: the text is used word for word (the model only writes the tags).
    """
    if not draft:
        return []
    if exact:
        return [
            '- The artist wrote this caption themselves and it must be used exactly, word for '
            f"word, on every site: {draft!r}",
            "- With a fixed caption like that, write only the tags (and the title where a site "
            "wants one): never rewrite, reword or reorder it.",
        ]
    return [
        "- The artist wrote a draft caption: keep its meaning, its tone and any call to action "
        f"(never drop them) and rewrite it better for each site: {draft!r}",
        "- Fix grammar and spelling, make it catchier, and add at most one or two fitting emoji.",
        "- Never add new claims or facts the artist did not write.",
        "- Adapt it per site: short for a short-form site, warmer where that fits, a Japanese "
        "rendering for Pixiv, and so on.",
    ]


def build_prompt(*, character: str, series: str = "", notes: str = "",
                 mature: bool = False, platforms=(), has_image: bool = True,
                 add_ai_tag: bool = True, caption_draft: str = "",
                 caption_exact: bool = False) -> str:
    # The title is capped by the strictest site taking part (Pixiv wants 32).
    title_limit = 50
    for platform_id in platforms:
        rule = SITE_RULES.get(platform_id) or {}
        title_limit = min(title_limit, int(rule.get("title_limit") or 50))
    lines = [
        "You write social-media captions for an artist who posts AI-generated illustrations.",
    ]
    if has_image:
        lines.append("Look at the image and describe what is actually in it.")
    else:
        lines.append(
            "No image is attached: write from the character, series and notes only, "
            "and do not describe poses, clothing or scenery."
        )
    lines += [
        "",
        f"Character: {character}",
    ]
    if series:
        lines.append(f"Series or game: {series}")
    if notes:
        lines.append(f"Artist's notes: {notes}")
    lines += [
        "",
        "Rules:",
        "- Reply with ONLY a JSON object, no markdown fences, in this shape:",
        '{"series": "...", "title": "...", "alt_text": "...", '
        '"platforms": {"<site>": {"caption": "...", "tags": ["..."]}}}',
        "- Use exactly these site keys: " + ", ".join(platforms),
        '- "series": the series or game the artwork is from, written properly (for example '
        '"Honkai: Star Rail"); use the series given above when there is one, otherwise "" if you '
        "cannot tell.",
        f'- "title": the English title, at most {title_limit} characters, no hashtags and no '
        "quotes.",
        '- Add a "title" inside a site entry only when that site asks for one, in the language it '
        "asks for.",
        "- Mention the character name in every caption.",
        "- Use a character's or series' official Japanese name only if you are certain it is the "
        "official one; otherwise keep the name exactly as the artist typed it. Never invent or "
        "guess a Japanese name.",
        "- When the series is known, include it as one tag with no spaces (for example HonkaiStarRail).",
        "- Include an AI-art tag such as aiart." if add_ai_tag else
        "- Do not add any AI-related tags.",
        "- Captions must not contain hashtags: put every tag in the tags array instead.",
        "- Never invent facts about the character: no made-up relationships, ages, powers or quotes.",
        "- Captions and alt text must never mention AI, AI-generated, generated, illustrations, "
        "prompts or tools, and never use the words mature, tasteful, suggestive or explicit: "
        "AI disclosure is handled by tags and site flags, never by the caption text.",
    ]
    lines += caption_rules(draft=caption_draft, exact=caption_exact)
    if has_image:
        lines.append("- alt_text: one or two plain sentences describing the image for screen readers.")
    else:
        lines.append("- alt_text: leave as an empty string (there is no image to describe).")
    lines += [
        "- Write in English, except for a site whose rules ask for another language.",
        "",
        "Per-site style:",
    ]
    for platform_id in platforms:
        rule = SITE_RULES.get(platform_id)
        if not rule:
            continue
        if rule.get("tags_in_text", True):
            ending = (f"the whole post (caption, blank line and tags) must fit "
                      f"{rule['limit']} characters.")
        else:
            ending = f"keep the caption under {rule['limit']} characters."
        line = f"- {platform_id}: {rule['caption']}; {rule['tags']}; {ending}"
        if rule.get("title_limit"):
            line += (f" It also needs its own title, in {rule.get('title_hint', 'English')}, "
                     f"at most {rule['title_limit']} characters.")
        if rule.get("no_ai_tags"):
            line += (" Never add an AI-related tag here (no aiart, AIアート, AIイラスト, AI生成…): "
                     "this site labels AI works itself.")
        if rule.get("character_tag") and character:
            line += (f' The artist\'s own spelling "{character}" is added as a tag automatically: '
                     f"if it is a short romanized name, prefer the official Japanese name (for "
                     f"example nino → 二乃) in the title and caption when you are certain of it.")
        lines.append(line)
    if mature:
        lines += [
            "",
            "The artwork is suggestive or mature: keep every word tasteful and non-explicit, "
            "and do not describe anatomy or anything explicit.",
        ]
    else:
        lines += ["", "The artwork is safe for work."]
    return "\n".join(lines)


def normalize(data: dict, platforms, character: str = "") -> dict:
    """Keep only the selected sites, clean up the fields, and fold stray hashtags
    the model left inside a caption into that site's tags list.

    Sites that need it also get their tags tidied: AI-disclosure tags are dropped
    where the site flags AI itself, and the artist's own spelling of the
    character is always one of the tags.
    """
    raw = data.get("platforms") if isinstance(data.get("platforms"), dict) else {}
    platform_out = {}
    for platform_id in platforms:
        entry = raw.get(platform_id) or {}
        if not isinstance(entry, dict):
            entry = {}
        caption, inline_tags = _strip_trailing_tags(str(entry.get("caption") or ""))
        tags = parse_tags([*parse_tags(entry.get("tags")), *inline_tags])
        rule = SITE_RULES.get(platform_id) or {}
        if rule.get("no_ai_tags"):
            tags = drop_ai_tags(tags)
        if rule.get("character_tag"):
            tags = with_character_tag(tags, character, max_tags=rule.get("max_tags"))
        site = with_parts({"caption": caption, "tags": tags}, entry)
        entry_title = str(entry.get("title") or "").strip()
        if entry_title:
            site["title"] = clip_title(entry_title, 50)
        platform_out[platform_id] = site
    return {
        "series": str(data.get("series") or "").strip(),
        "title": clip_title(str(data.get("title") or ""), 50),
        "alt_text": str(data.get("alt_text") or "").strip(),
        "platforms": platform_out,
    }


def drop_ai_tags(tags: list[str]) -> list[str]:
    """Remove AI-disclosure tags (a site with its own AI flag doesn't need one)."""
    return [tag for tag in tags if _normalized_tag(tag) not in AI_TAGS]


def as_tag(text: str) -> str:
    """One tag, never with whitespace: Pixiv's tag box splits on spaces."""
    return re.sub(r"\s+", "", str(text or "").strip())


def with_character_tag(tags: list[str], character: str,
                       max_tags: int | None = None) -> list[str]:
    """Tag the artist's own spelling of the character, exactly as typed.

    Whitespace is removed (`quintessential quintuplets` → `quintessential
    quintuplets` in one tag), because Pixiv's tag box would otherwise split the
    tag in two or waste a slot. The list is capped again after the insert.
    """
    tag = as_tag(character)
    if not tag:
        return tags
    if any(as_tag(existing).casefold() == tag.casefold() for existing in tags):
        return tags
    tags = [tag, *tags]
    return tags[:max_tags] if max_tags else tags


def _normalized_tag(tag: str) -> str:
    """`AI-Art`, `ai_art` and `AI アート` all compare as their bare spelling."""
    return re.sub(r"[\s_\-]+", "", str(tag)).casefold()


def _strip_trailing_tags(caption: str) -> tuple[str, list[str]]:
    """Remove hashtag lines / a trailing hashtag run from a caption.

    Returns the cleaned caption and the tag names that were removed.
    """
    lines = caption.split("\n")
    removed: list[str] = []

    while lines and (not lines[-1].strip() or _WHOLE_TAG_LINE_RE.match(lines[-1])):
        removed.extend(_TAG_WORD_RE.findall(lines.pop()))

    if lines:
        match = _TRAILING_TAGS_RE.search(lines[-1])
        if match:
            removed.extend(_TAG_WORD_RE.findall(match.group(1)))
            lines[-1] = lines[-1][: match.start(1)].rstrip()
            if not lines[-1].strip() and len(lines) > 1:
                lines.pop()

    return "\n".join(lines).strip(), removed


def parse_response(text: str) -> dict:
    """The model's reply as a dict: plain JSON, fenced JSON, or JSON in junk."""
    data = _try_json(text)
    if isinstance(data, dict):
        return data
    for candidate in _json_objects(text or ""):
        data = _try_json(candidate)
        if isinstance(data, dict):
            return data
    raise CaptionError("the model did not return the expected JSON")


def _try_json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _json_objects(text: str):
    """Yield each balanced {...} block, so junk before the real object is skipped."""
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return
        depth = 0
        in_string = False
        escaped = False
        for position in range(start, len(text)):
            char = text[position]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:position + 1]
                    index = position + 1
                    break
        else:
            return  # unbalanced: nothing more to find


def http_error_message(model: str, response) -> str:
    """A readable message for a non-200 reply from the provider.

    Callers scrub the result before showing it, so a stray secret can never
    reach the UI even if a server echoed one back.
    """
    hint = _STATUS_HINTS.get(response.status_code, "request failed")
    detail = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        elif error:
            detail = str(error)
        if not detail:
            detail = str(body.get("message") or "")
    if not detail:
        detail = str(getattr(response, "text", "") or "")
    text = f"{model}: HTTP {response.status_code} — {hint}"
    if detail:
        text += f" ({detail[:150]})"
    return text


async def _call(cfg: dict, model: str, messages: list, timeout: float) -> str:
    """One Chat Completions request. Isolated so tests can replace it.

    Timeouts, connection errors, 429 and 5xx become `CaptionUnavailable` (worth
    retrying); every other bad reply is a plain `CaptionError`.
    """
    payload = {"model": model, "messages": messages, "temperature": 0.7}
    headers = {
        "Authorization": f"Bearer {cfg.get('api_key', '')}",
        "User-Agent": USER_AGENT,
    }
    if provider(cfg) == "opencode":
        headers["x-opencode-session"] = _SESSION_ID
    endpoint = PROVIDERS[provider(cfg)]["endpoint"]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
            response = await client.post(endpoint, headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        raise CaptionUnavailable(f"{model}: timed out after {timeout:.0f}s") from exc
    except httpx.HTTPError as exc:
        raise CaptionUnavailable(f"{model}: connection failed ({type(exc).__name__})") from exc
    if response.status_code != 200:
        message = http_error_message(model, response)
        if _is_transient(response.status_code):
            raise CaptionUnavailable(message)
        raise CaptionError(message)
    try:
        body = response.json()
        message = body["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise CaptionError(f"{model}: unexpected response from {provider_label(cfg)}")
    content = message.get("content")
    if isinstance(content, list):  # some providers send content as parts
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content or "")


async def _attempt(cfg: dict, model: str, messages: list, timeout: float, attempt: int) -> str:
    """`_call` with the model, attempt number and elapsed seconds in the log."""
    started = time.monotonic()
    try:
        return await _call(cfg, model, messages, timeout)
    except CaptionError as exc:
        log.warning("caption %s attempt %s failed after %.1fs: %s",
                    model, attempt, time.monotonic() - started, exc)
        raise


def _image_part(path, max_bytes: int = MAX_IMAGE_BYTES,
                max_side: int = MAX_IMAGE_SIDE) -> dict:
    """One image as a small base64 JPEG data URL."""
    prepared = prepare(
        path,
        max_bytes=max_bytes,
        max_side=max_side,
        allow_original=False,  # always re-encode: the model gets a JPEG
    )
    encoded = base64.b64encode(prepared.data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}


def _spread(paths: list, limit: int) -> list:
    """Cover first, then evenly spread picks (for a mixed post's attachments)."""
    items = list(paths)
    if len(items) <= limit:
        return items
    step = (len(items) - 1) / (limit - 1) if limit > 1 else 0
    chosen = []
    for index in range(limit):
        position = round(index * step)
        if items[position] not in chosen:
            chosen.append(items[position])
    return chosen


def _messages(prompt: str, image_part=None) -> list:
    """One user turn: the prompt, then one image — or a list of them (mixed posts)."""
    parts = [{"type": "text", "text": prompt}]
    if isinstance(image_part, list):
        parts.extend(image_part)
    elif image_part is not None:
        parts.append(image_part)
    return [{"role": "user", "content": parts}]


async def generate(*, cfg: dict, images=(), character: str = "", series: str = "",
                   notes: str = "", mature: bool = False, platforms=(),
                   caption_draft: str = "", caption_exact: bool = False) -> dict:
    """Write a caption/tags/alt text for each selected site. Raises CaptionError."""
    _require_key(cfg)
    character = str(character or "").strip()
    if not character:
        raise CaptionError("Type the character name first.")
    platforms = list(platforms or [])
    if not platforms:
        raise CaptionError("Tick at least one site first.")
    caption_draft = str(caption_draft or "").strip()

    image = _image_part(list(images)[0]) if images else None

    prompt_base = dict(character=character, series=series, notes=notes,
                       mature=mature, platforms=platforms,
                       add_ai_tag=bool(cfg.get("add_ai_tag", True)),
                       caption_draft=caption_draft, caption_exact=bool(caption_exact))
    vision, backup = vision_model(cfg), backup_model(cfg)
    if image is not None:
        attempts = [(vision, True), (backup, True), (backup, False)]
    else:
        attempts = [(vision, False), (backup, False)]

    last_error = ""
    errors: list[Exception] = []
    for attempt, (model, with_image) in enumerate(attempts, start=1):
        # The name-only attempt gets the no-image prompt, so it cannot invent scenery.
        prompt = build_prompt(has_image=with_image, **prompt_base)
        try:
            raw = await _attempt(
                cfg, model, _messages(prompt, image if with_image else None),
                VISION_TIMEOUT if with_image else NAME_ONLY_TIMEOUT, attempt,
            )
            data = parse_response(raw)
        except Exception as exc:  # noqa: BLE001 — try the next attempt
            errors.append(exc)
            last_error = str(exc) if isinstance(exc, CaptionError) else f"{type(exc).__name__}: {exc}"
            continue
        result = normalize(data, platforms, character=character)
        if caption_draft and caption_exact:  # the artist's words win, verbatim
            result = dict(result)
            for entry in (result.get("platforms") or {}).values():
                if isinstance(entry, dict):
                    entry["caption"] = caption_draft
        result["used_model"] = model
        result["fallback"] = not with_image
        return result
    raise _final_error(last_error, errors)


async def test_connection(cfg: dict) -> str:
    """Ask each model the colour of a tiny solid image; it proves they see."""
    _require_key(cfg)
    image = _solid_colour_part()
    vision, backup = vision_model(cfg), backup_model(cfg)
    results = []
    for role, model in (("vision", vision), ("backup", backup)):
        messages = _messages("What colour fills this image? Reply with one word.", image)
        try:
            answer = await _call(cfg, model, messages, VISION_TIMEOUT)
        except CaptionError as exc:
            raise CaptionError(f"{role} model failed — {exc}")
        lowered = answer.lower()
        if not any(word in lowered for word in ("red", "crimson", "scarlet")):
            raise CaptionError(
                f"{role} model {model} answered {answer.strip()[:60]!r} — it did not see the image"
            )
        results.append(f"{role} {model} ✓")
    return "Caption AI ready — " + ", ".join(results)


def _solid_colour_part() -> dict:
    buffer = BytesIO()
    Image.new("RGB", (64, 64), (220, 30, 30)).save(buffer, format="JPEG", quality=85)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}
