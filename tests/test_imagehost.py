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


class _Answer:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def test_uguu_returns_the_direct_link(monkeypatch):
    monkeypatch.setattr(imagehost.httpx, "post", lambda *args, **kwargs: _Answer(
        200, {"success": True, "files": [{"url": "https://d.uguu.se/abc.jpg"}]}))

    assert imagehost.UguuHost().upload(b"x", "a.jpg").url == "https://d.uguu.se/abc.jpg"


def test_the_backup_host_takes_over_when_imgbb_is_down(monkeypatch):
    """Live: "imgbb upload failed: Imgbb is currently down for maintenance."""
    def fake_post(url, **kwargs):
        if "imgbb" in url:
            return _Answer(503, {"success": False, "error": {
                "message": "Imgbb is currently down for maintenance."}})
        return _Answer(200, {"success": True, "files": [{"url": "https://d.uguu.se/abc.jpg"}]})

    monkeypatch.setattr(imagehost.httpx, "post", fake_post)
    host = imagehost.FallbackHost([imagehost.ImgBBHost("k"), imagehost.UguuHost()])

    hosted = host.upload(b"x", "a.jpg")

    assert hosted.url == "https://d.uguu.se/abc.jpg"
    host.delete(hosted)  # goes to uguu's (no-op) delete, never imgbb's


def test_every_host_failing_names_each_reason(monkeypatch):
    monkeypatch.setattr(imagehost.httpx, "post", lambda url, **kwargs: _Answer(
        500, {"success": False, "error": {"message": "down"}, "description": "full"}))
    host = imagehost.FallbackHost([imagehost.ImgBBHost("k"), imagehost.UguuHost()])

    with pytest.raises(imagehost.ImageHostError) as caught:
        host.upload(b"x", "a.jpg")

    assert "imgbb" in str(caught.value) and "uguu" in str(caught.value)
