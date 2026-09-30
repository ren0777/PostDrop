"""Image preparation.

`prepare()` sends an original untouched when its format already fits a site's
limits, and otherwise re-encodes: transparency is flattened onto white and the
image becomes a JPEG, or — for the formats a site does not take at all, such as
WEBP on Pixiv — a lossless PNG (`png_formats`). The JPEG path tries quality
92→64, then scales down until it fits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

MIME_BY_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}
_QUALITIES = (92, 84, 76, 68, 64)
_PREFIX_RE = re.compile(r"^\d+_")
# Shared with the upload route in app/main.py — keep the two in step.
MAX_INPUT_BYTES = 32 * 1024 * 1024


@dataclass
class Prepared:
    data: bytes
    mime: str
    width: int
    height: int
    filename: str

    @property
    def size_kb(self) -> int:
        return max(1, round(len(self.data) / 1024))


def prepare(path, max_bytes: int, max_side: int | None = None,
            allow_original: bool = True, formats=None, png_formats=()) -> Prepared:
    """Return the bytes to upload for one image, within the given limits.

    `formats` lists the formats the site takes untouched; `png_formats` names
    the ones it cannot take at all and that must become a lossless PNG.
    """
    path = Path(path)
    raw = path.read_bytes()
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError(f"{path.name} is larger than {MAX_INPUT_BYTES // (1024 * 1024)} MB")

    with Image.open(BytesIO(raw)) as opened:
        opened.load()
        image_format = (opened.format or "").upper()
        mime = MIME_BY_FORMAT.get(image_format)
        if mime is None:
            raise ValueError(f"{path.name} is not a supported image")
        allowed = formats or tuple(MIME_BY_FORMAT)

        fits = len(raw) <= max_bytes and (max_side is None or max(opened.size) <= max_side)
        if allow_original and fits and image_format in allowed:
            return Prepared(raw, mime, opened.width, opened.height, _display_name(path))

        if image_format in png_formats:
            data, width, height = _encode_png_to_fit(
                ImageOps.exif_transpose(opened), max_bytes, max_side)
            return Prepared(data, "image/png", width, height, _display_name(path, ".png"))

        working = _flatten(ImageOps.exif_transpose(opened))
        if max_side is not None and max(working.size) > max_side:
            working = _scale(working, max_side)
        data, width, height = _encode_to_fit(working, max_bytes)

    return Prepared(data, "image/jpeg", width, height, _display_name(path, ".jpg"))


def _display_name(path: Path, extension: str | None = None) -> str:
    """Upload file name without the ordering prefix added by the uploader."""
    name = _PREFIX_RE.sub("", path.name)
    if extension is None:
        return name
    return Path(name).stem + extension


def _flatten(image: Image.Image) -> Image.Image:
    if image.mode == "P":
        image = image.convert("RGBA")
    if image.mode in ("RGBA", "LA", "PA"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _scale(image: Image.Image, max_side: int) -> Image.Image:
    width, height = image.size
    factor = max_side / max(width, height)
    return image.resize(
        (max(1, round(width * factor)), max(1, round(height * factor))),
        Image.LANCZOS,
    )


def _encode_to_fit(image: Image.Image, max_bytes: int):
    working = image
    for _ in range(40):
        for quality in _QUALITIES:
            buffer = BytesIO()
            working.save(buffer, format="JPEG", quality=quality, optimize=True)
            data = buffer.getvalue()
            if len(data) <= max_bytes:
                return data, working.width, working.height
        if min(working.size) <= 64:
            break
        working = working.resize(
            (max(1, int(working.width * 0.85)), max(1, int(working.height * 0.85))),
            Image.LANCZOS,
        )
    raise ValueError(f"could not compress the image below {max_bytes} bytes")


def _encode_png_to_fit(image: Image.Image, max_bytes: int, max_side: int | None):
    """Lossless PNG, scaled down until it fits (Pixiv does not take WEBP)."""
    working = image
    if working.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        working = working.convert("RGBA")
    if max_side is not None and max(working.size) > max_side:
        working = _scale(working, max_side)
    for _ in range(40):
        buffer = BytesIO()
        working.save(buffer, format="PNG", optimize=True)
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            return data, working.width, working.height
        if min(working.size) <= 64:
            break
        working = working.resize(
            (max(1, int(working.width * 0.85)), max(1, int(working.height * 0.85))),
            Image.LANCZOS,
        )
    raise ValueError(f"could not compress the image below {max_bytes} bytes")


def pad_to_ratio(prepared: Prepared, min_ratio: float = 0.8, max_ratio: float = 1.91,
                 max_bytes: int = 8_000_000, max_side: int | None = 1440) -> Prepared:
    """Pad with white so the aspect ratio sits inside [min_ratio, max_ratio].

    Instagram rejects (or crops) images outside 4:5 … 1.91:1, so the artwork
    keeps its size and gains white bars instead.
    """
    with Image.open(BytesIO(prepared.data)) as opened:
        opened.load()
        width, height = opened.size
        ratio = width / height
        if min_ratio <= ratio <= max_ratio:
            return prepared
        working = _flatten(ImageOps.exif_transpose(opened))
        if ratio < min_ratio:
            new_width = round(height * min_ratio)
            canvas = Image.new("RGB", (new_width, height), (255, 255, 255))
            canvas.paste(working, ((new_width - width) // 2, 0))
        else:
            new_height = round(width / max_ratio)
            canvas = Image.new("RGB", (width, new_height), (255, 255, 255))
            canvas.paste(working, (0, (new_height - height) // 2))
        if max_side is not None and max(canvas.size) > max_side:
            canvas = _scale(canvas, max_side)
        data, new_width, new_height = _encode_to_fit(canvas, max_bytes)
    return Prepared(data, "image/jpeg", new_width, new_height, prepared.filename)
