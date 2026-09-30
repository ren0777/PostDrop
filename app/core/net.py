"""Network helpers shared by the connectors."""
from __future__ import annotations

import ssl

_contexts: dict[bool, ssl.SSLContext] = {}
# X25519 is the preferred narrow curve; some OpenSSL builds only know prime256v1.
NARROW_CURVES = ("X25519", "prime256v1")


def tls_context(narrow: bool = False) -> ssl.SSLContext:
    """An SSL context, optionally pinned to a single ECDH curve.

    api.tumblr.com drops the default OpenSSL ClientHello on some machines
    (Python 3.14 / OpenSSL 3.0), so Tumblr's traffic uses one narrow curve.
    Every other site keeps the default context.
    """
    context = _contexts.get(narrow)
    if context is None:
        context = ssl.create_default_context()
        if narrow:
            for curve in NARROW_CURVES:
                try:
                    context.set_ecdh_curve(curve)
                    break
                except (ssl.SSLError, ValueError):
                    continue
        _contexts[narrow] = context
    return context
