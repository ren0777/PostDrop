"""Image preparation: passthrough, transparency flattening, size limits."""
from io import BytesIO

from PIL import Image

from app.core.images import pad_to_ratio, prepare


def _save(tmp_path, name, image, image_format=None, **kwargs):
    path = tmp_path / name
    image.save(path, format=image_format, **kwargs)
    return path


def test_passthrough_keeps_original_bytes(tmp_path):
    path = _save(tmp_path, "small.jpg", Image.new("RGB", (400, 300), (120, 40, 90)), "JPEG", quality=90)
    prepared = prepare(path, max_bytes=10_000_000, max_side=2000)
    assert prepared.data == path.read_bytes()
    assert prepared.mime == "image/jpeg"
    assert (prepared.width, prepared.height) == (400, 300)


def test_reencodes_transparency_to_jpeg(tmp_path):
    path = _save(tmp_path, "alpha.png", Image.new("RGBA", (800, 600), (0, 0, 0, 0)), "PNG")
    prepared = prepare(path, max_bytes=10_000_000, max_side=2000, formats=("JPEG",))
    assert prepared.mime == "image/jpeg"
    assert Image.open(BytesIO(prepared.data)).mode == "RGB"
    assert prepared.filename == "alpha.jpg"


def test_webp_becomes_png_when_the_site_does_not_take_it(tmp_path):
    # Pixiv's file input takes JPEG/PNG/GIF only
    path = _save(tmp_path, "01_art.webp", Image.new("RGBA", (120, 90), (10, 120, 200, 128)), "WEBP")
    prepared = prepare(path, max_bytes=8_000_000, formats=("JPEG", "PNG", "GIF"),
                       png_formats=("WEBP",))
    assert prepared.mime == "image/png"
    assert prepared.filename == "art.png"
    assert Image.open(BytesIO(prepared.data)).format == "PNG"
    assert Image.open(BytesIO(prepared.data)).mode == "RGBA"  # kept, not flattened


def test_max_side_scales_down(tmp_path):
    path = _save(tmp_path, "big.png", Image.new("RGB", (3000, 2000), (10, 120, 200)), "PNG")
    prepared = prepare(path, max_bytes=10_000_000, max_side=1000)
    assert prepared.width <= 1000
    assert prepared.height <= 1000
    assert prepared.mime == "image/jpeg"


def test_compresses_under_byte_limit(tmp_path):
    path = _save(tmp_path, "noise.png", Image.effect_noise((1200, 1200), 64).convert("RGB"), "PNG")
    prepared = prepare(path, max_bytes=80_000)
    assert len(prepared.data) <= 80_000


def test_filename_strips_upload_order_prefix(tmp_path):
    path = _save(tmp_path, "01_art.png", Image.new("RGB", (50, 50)), "PNG")
    assert prepare(path, max_bytes=1_000_000, max_side=100).filename == "art.png"


def test_pad_to_ratio_adds_white_bars(tmp_path):
    path = _save(tmp_path, "tall.png", Image.new("RGB", (500, 1000), (10, 60, 120)), "PNG")
    prepared = prepare(path, max_bytes=8_000_000, max_side=1440, allow_original=False)
    padded = pad_to_ratio(prepared)
    assert abs(padded.width / padded.height - 0.8) < 0.02
    assert Image.open(BytesIO(padded.data)).size == (padded.width, padded.height)


def test_pad_to_ratio_leaves_compliant_images_alone(tmp_path):
    path = _save(tmp_path, "square.png", Image.new("RGB", (600, 600), (10, 60, 120)), "PNG")
    prepared = prepare(path, max_bytes=8_000_000, max_side=1440, allow_original=False)
    assert pad_to_ratio(prepared) is prepared


def test_pad_to_ratio_handles_very_wide_images(tmp_path):
    path = _save(tmp_path, "wide.png", Image.new("RGB", (2000, 500), (10, 60, 120)), "PNG")
    prepared = prepare(path, max_bytes=8_000_000, max_side=1440, allow_original=False)
    padded = pad_to_ratio(prepared)
    assert abs(padded.width / padded.height - 1.91) < 0.02
    assert max(padded.width, padded.height) <= 1440


def test_pad_to_ratio_shrinks_oversized_padding(tmp_path):
    path = _save(tmp_path, "tall.png", Image.new("RGB", (1000, 2000), (10, 60, 120)), "PNG")
    prepared = prepare(path, max_bytes=8_000_000, max_side=1440, allow_original=False)
    padded = pad_to_ratio(prepared)
    assert max(padded.width, padded.height) <= 1440
    assert abs(padded.width / padded.height - 0.8) < 0.02
