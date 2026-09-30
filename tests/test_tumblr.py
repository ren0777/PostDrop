"""Tumblr: blog names, NPF payloads, tag handling, the narrow TLS context."""
import asyncio

import httpx
import pytest
from PIL import Image

from app.core import net
from app.core.images import Prepared
from app.core.post import Post
from app.platforms import REGISTRY
from app.platforms.base import PublishError
from app.platforms.tumblr import Tumblr, blog_host, build_npf, post_url


def _prepared(name="art.jpg"):
    return Prepared(data=b"jpeg-bytes", mime="image/jpeg", width=800, height=600, filename=name)


def test_blog_host_accepts_short_and_full_names():
    assert blog_host("myname") == "myname.tumblr.com"
    assert blog_host("@myname") == "myname.tumblr.com"
    assert blog_host("myname.tumblr.com") == "myname.tumblr.com"
    assert blog_host("") == ""


def test_build_npf_makes_image_then_text_blocks():
    payload, files = build_npf("hello", ["art", "aiart"], False, [_prepared()], alt="alt text")
    assert payload["state"] == "published"
    assert payload["tags"] == "art,aiart"
    assert payload["content"][0]["type"] == "image"
    assert payload["content"][0]["media"][0]["identifier"] == "media1"
    assert payload["content"][0]["alt_text"] == "alt text"
    assert payload["content"][1] == {"type": "text", "text": "hello"}
    assert files["media1"][0] == "art.jpg"


def test_build_npf_as_a_draft_without_text():
    payload, _ = build_npf("", [], True, [_prepared()])
    assert payload["state"] == "draft"
    assert payload["tags"] == ""
    assert [block["type"] for block in payload["content"]] == ["image"]


def test_post_url_only_for_published_posts():
    assert post_url("myname", "123", True) == "https://myname.tumblr.com/post/123"
    assert post_url("myname.tumblr.com", "123", True) == "https://myname.tumblr.com/post/123"
    assert post_url("myname", "123", False) == ""


def test_dry_run_keeps_hashtags_out_of_the_caption(tmp_path):
    path = tmp_path / "art.png"
    Image.new("RGB", (64, 64)).save(path, "PNG")
    post = Post(images=[path], caption="hello", tags=["art", "aiart"],
                options={"tumblr": {"style": "ai"}})
    result = asyncio.run(Tumblr().publish(post, {}, dry_run=True))
    assert result.ok is True
    assert result.preview["text"] == "hello"  # tags go in Tumblr's own field


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_json_helper_accepts_201_but_rejects_error_bodies():
    connector = Tumblr()
    body = {"meta": {"status": 201}, "response": {"id_string": "1"}}
    assert connector._json(_Response(201, body), "x") == {"id_string": "1"}

    with pytest.raises(PublishError):
        connector._json(_Response(200, {"meta": {"status": 404, "msg": "Not found"},
                                        "errors": [{"detail": "gone"}]}), "x")


def test_tumblr_clients_use_the_narrow_tls_context(monkeypatch):
    captured = {}

    def fake_client(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    Tumblr()._client(httpx.Timeout(5))
    assert captured["verify"] is net.tls_context(True)


def test_only_tumblr_asks_for_narrow_tls():
    assert REGISTRY["tumblr"].oauth_provider.narrow_tls is True
    assert REGISTRY["deviantart"].oauth_provider.narrow_tls is False
