"""Secret scrubbing and pre-flight checks."""
import asyncio
from pathlib import Path

from PIL import Image

from app.core.post import Post
from app.platforms import REGISTRY
from app.platforms.base import Connector, Field, scrub
from app.platforms.bluesky import Bluesky


class _Fake(Connector):
    fields = [Field("token", "Token", secret=True), Field("label", "Label")]


def test_scrub_removes_secret_values():
    message = "failed: https://api.example.com/abc123/do?x=1"
    scrubbed = scrub(message, {"token": "abc123", "label": "hello"}, _Fake())
    assert "abc123" not in scrubbed
    assert "***" in scrubbed


def test_scrub_keeps_non_secret_values():
    scrubbed = scrub("label is hello", {"token": "abc123", "label": "hello"}, _Fake())
    assert "hello" in scrubbed


def test_scrub_removes_access_tokens_from_urls():
    message = "failed calling https://graph.instagram.com/me?access_token=IGQ1234567890"
    scrubbed = scrub(message, {"tokens": {"access_token": "IGQ1234567890"}}, _Fake())
    assert "IGQ1234567890" not in scrubbed
    assert "***" in scrubbed


def test_dry_run_reports_missing_settings(tmp_path):
    path = tmp_path / "one.png"
    Image.new("RGB", (64, 64)).save(path, format="PNG")
    post = Post(images=[path], caption="x")
    result = asyncio.run(Bluesky().publish(post, {}, dry_run=True))
    # check() passes (1 image), prepare() runs, and "ready" is False with no config.
    assert result.ok is True
    assert result.preview["ready"] is False
    assert result.preview["missing"] == ["Handle", "App password"]


def test_per_site_tags_override_reaches_the_caption(tmp_path):
    path = tmp_path / "one.png"
    Image.new("RGB", (64, 64)).save(path, "PNG")
    post = Post(images=[path], caption="hello", tags=["shared"],
                options={"discord": {"tags": "one two"}})
    result = asyncio.run(REGISTRY["discord"].publish(post, {}, dry_run=True))
    assert result.ok is True
    assert result.preview["text"] == "hello\n\n#one #two"


def test_shared_tags_used_without_an_override(tmp_path):
    path = tmp_path / "one.png"
    Image.new("RGB", (64, 64)).save(path, "PNG")
    post = Post(images=[path], caption="hello", tags=["shared"])
    result = asyncio.run(REGISTRY["discord"].publish(post, {}, dry_run=True))
    assert result.preview["text"] == "hello\n\n#shared"
