"""The TLS context helper behind the Tumblr handshake fix."""
import ssl

import pytest

from app.core import net


@pytest.fixture(autouse=True)
def fresh_contexts():
    net._contexts.clear()
    yield
    net._contexts.clear()


def test_narrow_context_pins_the_curve(monkeypatch):
    calls = []

    class FakeContext:
        def set_ecdh_curve(self, name):
            calls.append(name)

    monkeypatch.setattr(ssl, "create_default_context", lambda: FakeContext())
    net.tls_context(False)
    net.tls_context(True)
    assert calls == ["X25519"]


def test_narrow_context_falls_back_to_prime256v1(monkeypatch):
    calls = []

    class FakeContext:
        def set_ecdh_curve(self, name):
            if name == "X25519":
                raise ssl.SSLError("unknown group")
            calls.append(name)

    monkeypatch.setattr(ssl, "create_default_context", lambda: FakeContext())
    net.tls_context(True)
    assert calls == ["prime256v1"]


def test_narrow_context_works_on_this_machine():
    context = net.tls_context(True)  # must never raise
    assert isinstance(context, ssl.SSLContext)


def test_contexts_are_cached_and_distinct():
    narrow = net.tls_context(True)
    plain = net.tls_context(False)
    assert narrow is net.tls_context(True)
    assert plain is net.tls_context(False)
    assert narrow is not plain
