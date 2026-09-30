"""The imgbb image host behind Instagram."""
import pytest

from app.core import imagehost


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_imgbb_upload_returns_the_url_and_delete_link(monkeypatch):
    calls = {}

    def fake_post(url, params=None, files=None, timeout=None):
        calls.update({"url": url, "params": params, "files": files})
        return _Response(200, {"success": True, "data": {"url": "https://i.ibb.co/x/a.jpg",
                                                         "delete_url": "https://ibb.co/x/1"}})

    monkeypatch.setattr(imagehost.httpx, "post", fake_post)
    hosted = imagehost.ImgBBHost("key-123").upload(b"jpeg-bytes", "art.jpg")
    assert hosted.url == "https://i.ibb.co/x/a.jpg"
    assert hosted.delete_url == "https://ibb.co/x/1"
    assert calls["params"] == {"key": "key-123", "expiration": 3600}
    assert calls["files"]["image"][0] == "art.jpg"


def test_imgbb_upload_reports_errors(monkeypatch):
    monkeypatch.setattr(
        imagehost.httpx, "post",
        lambda *args, **kwargs: _Response(400, {"success": False,
                                                "error": {"message": "Invalid API key"}}),
    )
    with pytest.raises(imagehost.ImageHostError):
        imagehost.ImgBBHost("bad").upload(b"x", "a.jpg")


def test_imgbb_delete_is_a_documented_noop(monkeypatch):
    called = []
    monkeypatch.setattr(imagehost.httpx, "get", lambda *args, **kwargs: called.append(True))
    imagehost.ImgBBHost("k").delete(imagehost.HostedImage("u", "https://ibb.co/x/1"))
    assert called == []
