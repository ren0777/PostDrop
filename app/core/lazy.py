"""Lazy mode (M6): folders in, queue out.

The user drops images into `Pictures\\ArtDrop\\outbox` (or `outbox-instagram`,
which is allowed to reach Instagram). Files that land together form a **batch**:
same folder, within `BATCH_WINDOW_SECONDS` of each other, posted in file-name
order with the first image as the cover. Subfolders are batches of their own.

Everything non-secret lives in `data/settings.json` under `"lazy"`, next to the
caption styles — never secrets, which stay in the keyring.
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from . import paths, settings

BATCH_WINDOW_SECONDS = 180.0  # files dropped this close together are one batch
SETTLE_SECONDS = 30.0  # a fresh file waits this long before it is picked up
MIN_SPACING_MINUTES = 30

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")
IGNORED_DIRS = ("posted",)
IGNORED_PREFIXES = (".", "~$")
IGNORED_SUFFIXES = (".part", ".tmp", ".crdownload", ".json", ".txt")

# Which folders are watched, what they may reach, and how mature they are.
DEFAULT_FOLDERS = (
    {"key": "outbox", "path": "outbox", "mature": True, "pixiv": True,
     "cards": ["bluesky", "telegram", "discord", "tumblr", "deviantart"]},
    {"key": "outbox-instagram", "path": "outbox-instagram", "mature": False, "pixiv": True,
     "cards": ["bluesky", "telegram", "discord", "tumblr", "deviantart",
               "instagram:main"]},
)

# Per site defaults: images per post and the spacing between posts.
SITE_LIMITS = {
    "bluesky": {"per_post": 4, "spacing_minutes": 120},
    "telegram": {"per_post": 10, "spacing_minutes": 120},
    "discord": {"per_post": 10, "spacing_minutes": 120},
    "tumblr": {"per_post": 10, "spacing_minutes": 120},
    "instagram": {"per_post": 10, "spacing_minutes": 120},
    "deviantart": {"per_post": 1, "spacing_minutes": 120},
}
DEFAULT_SPACING_MINUTES = 120
QUIET_DEFAULTS = {"enabled": False, "start": 1, "end": 8}


# ------------------------------------------------------------------- settings
def root() -> Path:
    """Where the outbox folders live (`Pictures\\ArtDrop`)."""
    return Path.home() / "Pictures" / "ArtDrop"


def resolve(path: str) -> Path:
    """An absolute path, or one relative to the ArtDrop pictures folder."""
    raw = str(path or "").strip()
    candidate = Path(raw).expanduser() if raw else Path()
    return candidate if candidate.is_absolute() else root() / candidate


def stored() -> dict:
    """The saved lazy-mode settings with every default filled in."""
    data = settings.load().get("lazy")
    data = data if isinstance(data, dict) else {}
    folders = data.get("folders")
    folders = folders if isinstance(folders, list) and folders else list(DEFAULT_FOLDERS)
    sites = data.get("sites")
    sites = dict(sites) if isinstance(sites, dict) else {}
    quiet = data.get("quiet")
    quiet = dict(quiet) if isinstance(quiet, dict) else {}
    return {
        "enabled": bool(data.get("enabled")),
        "paused": bool(data.get("paused")),
        "folders": [
            {
                "key": str(folder.get("key") or "outbox"),
                "path": str(folder.get("path") or "outbox"),
                "mature": bool(folder.get("mature")),
                "pixiv": bool(folder.get("pixiv", True)),
                "cards": [str(card) for card in (folder.get("cards") or [])],
            }
            for folder in folders
            if isinstance(folder, dict)
        ],
        "sites": {**{site: dict(limits) for site, limits in SITE_LIMITS.items()},
                  **{str(site): dict(limits) for site, limits in sites.items()
                     if isinstance(limits, dict)}},
        "quiet": {**QUIET_DEFAULTS, **{key: quiet[key] for key in QUIET_DEFAULTS
                                       if key in quiet}},
    }


def save(data: dict) -> dict:
    """Validate and store the lazy-mode settings; returns what is stored."""
    current = stored()
    if isinstance(data, dict):
        if "enabled" in data:
            current["enabled"] = bool(data["enabled"])
        if "paused" in data:
            current["paused"] = bool(data["paused"])
        if isinstance(data.get("folders"), list):
            current["folders"] = _clean_folders(data["folders"])
        if isinstance(data.get("sites"), dict):
            current["sites"] = _clean_sites(data["sites"], current["sites"])
        if isinstance(data.get("quiet"), dict):
            current["quiet"] = _clean_quiet(data["quiet"], current["quiet"])
    whole = settings.load()
    whole["lazy"] = current
    settings.save(whole)
    return current


def _clean_folders(folders: list) -> list[dict]:
    cleaned = []
    for folder in folders:
        if not isinstance(folder, dict):
            continue
        cleaned.append({
            "key": str(folder.get("key") or "outbox"),
            "path": str(folder.get("path") or "outbox").strip(),
            "mature": bool(folder.get("mature")),
            "pixiv": bool(folder.get("pixiv", True)),
            "cards": [str(card) for card in (folder.get("cards") or [])],
        })
    return cleaned or list(DEFAULT_FOLDERS)


def _clean_sites(sites: dict, current: dict) -> dict:
    for site, limits in sites.items():
        if not isinstance(limits, dict):
            continue
        merged = {**current.get(str(site), SITE_LIMITS.get(str(site), {})), **limits}
        per_post = max(1, int(merged.get("per_post") or 1))
        spacing = max(MIN_SPACING_MINUTES, int(merged.get("spacing_minutes")
                                              or DEFAULT_SPACING_MINUTES))
        current[str(site)] = {"per_post": per_post, "spacing_minutes": spacing}
    return current


def _clean_quiet(quiet: dict, current: dict) -> dict:
    merged = {**current, **quiet}
    return {
        "enabled": bool(merged.get("enabled")),
        "start": min(23, max(0, int(merged.get("start") or 0))),
        "end": min(24, max(0, int(merged.get("end") or 0))),
    }


def limits_for(site: str, data: dict | None = None) -> dict:
    """The chunk size and spacing for one site id (an account uses its site's)."""
    data = data or stored()
    return dict(data["sites"].get(site) or SITE_LIMITS.get(site)
                or {"per_post": 10, "spacing_minutes": DEFAULT_SPACING_MINUTES})


# -------------------------------------------------------------------- batches
MIX_FOLDER_RE = re.compile(r"^(mix|mixed)([\s_-]|$)", re.IGNORECASE)
# Only explicit counter forms count as a "this is post number N" marker: a plain
# space-separated number may be part of the name (Android 18, Area 51), so it stays.
COUNTER_FORMS = re.compile(
    r"(?:"
    r"[\s_\-]*\(\d+\)"   # Changli (2)
    r"|[\s_\-]*\[\d+\]"    # Changli [2]
    r"|[\s_\-]*#\s*\d+"   # Changli #2
    r"|[\s_\-]*v\d+"      # Changli v2
    r"|[_\-]\d+"        # Changli_2, Changli-2
    r")$",
    re.IGNORECASE,
)


def is_mix_folder(name: str) -> bool:
    """True for `mix`, `mixed`, `mix-anything`, `mixed_set`… (a deliberate mixed post)."""
    return bool(MIX_FOLDER_RE.match(str(name or "").strip()))


def artist_name(name: str) -> str:
    """A folder name as the character's name.

    `Changli (2)`, `Changli [2]`, `Changli_2`, `Changli-2`, `Changli #2` and
    `Changli v2` all mean "Changli" (one counter is stripped). A plain
    `Changli 2` is left alone — the number may be part of the name, like
    `Android 18`, `Area 51` or `March 7th`. A `Character - Series` name gives
    only the character here; see `folder_identity`.
    """
    return folder_identity(name)[0]


# "Citlali - Genshin Impact", "Citlali- genshin impact", "Citlali -Genshin Impact":
# a dash with a space on at least one side. "Hu-Tao" has none, so it never splits.
SERIES_SPLIT = re.compile(r"\s+-\s*|\s*-\s+")


def folder_identity(name: str) -> tuple[str, str]:
    """A subfolder name as (character, series).

    `Citlali - Genshin Impact (2)` → ("Citlali", "Genshin Impact"). The split is
    on the first spaced dash; an all-lowercase series is title-cased, any other
    casing is the artist's and is kept. Without a series part the series is "".
    """
    raw = str(name or "").strip()
    stripped = COUNTER_FORMS.sub("", raw).strip() or raw  # once, on the whole name
    parts = SERIES_SPLIT.split(stripped, maxsplit=1)
    series = ""
    head = stripped
    if len(parts) == 2 and parts[0].strip() and parts[1].strip() \
            and not parts[1].strip().isdigit():
        head = COUNTER_FORMS.sub("", parts[0].strip()).strip() or parts[0].strip()
        series = _series_case(re.sub(r"\s+", " ", parts[1].replace("_", " ")).strip())
    character = re.sub(r"\s+", " ", head.replace("_", " ").replace("-", " ")).strip()
    return character or raw, series


def _series_case(series: str) -> str:
    """"genshin impact" → "Genshin Impact"; "Honkai: Star Rail" stays as typed."""
    if series != series.lower():
        return series
    return " ".join(word[:1].upper() + word[1:] for word in series.split(" "))


def is_ignored_dir(name: str) -> bool:
    """Folders starting with `_` are the artist's own notes: never scanned."""
    return str(name or "").startswith("_")


def is_image(path: Path) -> bool:
    name = path.name.lower()
    if any(name.startswith(prefix) for prefix in IGNORED_PREFIXES):
        return False
    if any(name.endswith(suffix) for suffix in IGNORED_SUFFIXES):
        return False
    return path.suffix.lower() in IMAGE_SUFFIXES


def files(folder: Path) -> list[Path]:
    """Every dropped image under a folder, `posted/` and temp files aside."""
    if not folder.exists():
        return []
    found = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or not is_image(path):
            continue
        parts = path.relative_to(folder).parts[:-1]
        if any(part.lower() in IGNORED_DIRS for part in parts):
            continue
        if any(is_ignored_dir(part) for part in parts):
            continue
        found.append(path)
    return found


def batches(folder: Path, now: float | None = None,
            settle: float = SETTLE_SECONDS) -> list[list[Path]]:
    """Groups of files that landed together, oldest batch first.

    Same directory + consecutive arrivals within the batch window; the order
    inside a batch is file-name order (the first is the cover). Subfolders are
    their own groups, and a batch is only returned once it has settled.
    """
    now = time.time() if now is None else now
    groups: dict[Path, list[Path]] = {}
    for path in files(folder):
        groups.setdefault(path.parent, []).append(path)

    out: list[list[Path]] = []
    for group in groups.values():
        group.sort(key=lambda path: (path.stat().st_mtime, path.name.lower()))
        run: list[Path] = []
        previous = 0.0
        for path in group:
            mtime = path.stat().st_mtime
            if run and mtime - previous > BATCH_WINDOW_SECONDS:
                out.append(run)
                run = []
            run.append(path)
            previous = mtime
        if run:
            out.append(run)

    ready = [batch for batch in out if now - batch[-1].stat().st_mtime >= settle]
    ready.sort(key=lambda batch: batch[0].stat().st_mtime)
    return ready


def batch_key(folder: Path, batch: list[Path]) -> str:
    """A stable name for a batch: its folder, cover name and content hash."""
    return set_key(folder, batch)


def set_key(folder: Path, images: list[Path], group_key: str = "") -> str:
    """A set's stable name: folder, cover and content, plus what it was split on."""
    cover = images[0]
    suffix = f"+{group_key[:24]}" if group_key else ""
    # a subfolder set carries its folder too: a renamed folder is a new set
    where = folder.name if cover.parent == folder else f"{folder.name}/{cover.parent.name}"
    return f"{where}/{cover.name}{suffix}:{digest(cover)[:16]}"


# ------------------------------------------------------- splitting by character
STOP_WORDS = ("the",)


def normalize_name(value: str) -> str:
    """A name as a key: lowercase, no punctuation, no spaces, no "the"."""
    text = str(value or "").casefold()
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    return "".join(word for word in text.split() if word not in STOP_WORDS)


def group_key(series: str, characters) -> str:
    """Loose images that share this key belong to one set (docs/ARCHITECTURE.md).

    Normalized series plus the sorted normalized character names, so a duo or a
    group piece is always its own set.
    """
    names = sorted({normalize_name(name) for name in (characters or []) if str(name).strip()})
    names = [name for name in names if name]
    return f"{normalize_name(series)}|{'|'.join(names)}"


def unusable(folder: Path) -> list[str]:
    """Files sitting in a folder that ArtDrop will never pick up (the "Stuck" list).

    `.txt` sidecars are a known convention and part-downloads are still arriving,
    so neither of those counts; anything else that is not an image does.
    """
    if not folder.exists():
        return []
    found: list[str] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(folder)
        if any(part.lower() in IGNORED_DIRS for part in relative.parts[:-1]):
            continue
        if any(is_ignored_dir(part) for part in relative.parts[:-1]):
            continue
        name = relative.name.lower()
        if any(name.startswith(prefix) for prefix in IGNORED_PREFIXES):
            continue
        if path.suffix.lower() in (".txt", ".part", ".tmp", ".crdownload"):
            continue
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            found.append(relative.as_posix())
    return found


def digest(path: Path) -> str:
    """The file's content hash — the double-post guard for lazy mode."""
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def chunks(images: list[Path], per_post: int) -> list[list[Path]]:
    """Split a batch into posts of at most `per_post` images (never empty)."""
    size = max(1, int(per_post or 1))
    return [images[index:index + size] for index in range(0, len(images), size)]


def free_name(parent: Path, name: str) -> str:
    """A folder name that is not taken yet: `Changli`, then `Changli-2`, `Changli-3`…"""
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", str(name or "").strip()).strip(" .")
    if not cleaned:
        return "set"
    candidate = cleaned
    counter = 2
    while (parent / candidate).exists():
        candidate = f"{cleaned}-{counter}"
        counter += 1
    return candidate


def move_to_posted(batch_files: list[Path], target_root: Path | None = None,
                   name: str = "") -> Path:
    """Move a finished set into `posted\\<date>\\<name>\\`, keeping the file names.

    A subfolder set keeps its folder name so `posted/` stays readable; loose sets
    use their character name (or "Universal"). A name already used that day gets
    `-2`, `-3`…, and a file-name clash inside the folder gets `-2` too.
    """
    day = (target_root or root()) / "posted" / time.strftime("%Y-%m-%d")
    folder = day / free_name(day, name) if name else day
    folder.mkdir(parents=True, exist_ok=True)
    for path in batch_files:
        source = Path(path)
        if not source.exists():
            continue
        destination = folder / source.name
        counter = 2
        while destination.exists():
            destination = folder / f"{source.stem}-{counter}{source.suffix}"
            counter += 1
        source.replace(destination)
    return folder


def remove_if_empty(directories, roots=()) -> list[Path]:
    """Delete the set folders a move emptied, then their empty parents.

    Never touches a configured outbox root or an `_`-prefixed folder, and never
    removes a folder that still holds anything (a stray file keeps it).
    """
    protected = set()
    for item in roots:
        try:
            protected.add(Path(item).resolve())
        except OSError:  # pragma: no cover — a broken path is simply not protected
            continue
    removed: list[Path] = []
    for directory in {Path(item).resolve() for item in directories}:
        while True:
            if directory in protected or is_ignored_dir(directory.name):
                break
            try:
                if not directory.is_dir() or any(directory.iterdir()):
                    break
                directory.rmdir()
            except OSError:  # a folder that will not go is left alone
                break
            removed.append(directory)
            directory = directory.parent
    return removed


def sidecars(directory: Path) -> list[Path]:
    """The plain-text bits that ride along with a set (a `caption.txt`)."""
    if not directory.is_dir():
        return []
    return [path for path in sorted(directory.iterdir())
            if path.is_file() and path.suffix.lower() == ".txt"]


def caption_draft(directory: Path) -> tuple[str, bool]:
    """This set's own caption text and whether it is exact (`!exact` first line).

    The first line `!exact` (case-insensitive, whitespace ignored) switches from
    polish mode — the model rewrites the draft per site — to word-for-word use.
    """
    path = directory / "caption.txt"
    if not path.is_file():
        return "", False
    text = ""
    for encoding in ("utf-8", "utf-8-sig", "cp932"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except (OSError, UnicodeDecodeError):
            continue
    lines = text.splitlines()
    exact = False
    if lines and lines[0].strip().casefold() == "!exact":
        exact = True
        lines = lines[1:]
    return "\n".join(lines).strip(), exact


def ensure_folders(data: dict | None = None) -> list[Path]:
    """Create the outbox folders (and `posted/`) if they are missing."""
    data = data or stored()
    created: list[Path] = []
    for folder in data["folders"]:
        path = resolve(folder["path"])
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:  # noqa: PERF203 — an unmountable Pictures folder is not fatal
            continue
        created.append(path)
    try:
        (root() / "posted").mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return created
