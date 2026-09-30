"""Credential storage.

Secrets live in the OS keyring — on Windows that is the Credential Manager
(service "ArtDrop", one JSON blob per site), never in a plain-text file.
Everything here is import-safe on Linux; only the calls need a keyring.
"""
from __future__ import annotations

import json
import logging

import keyring

log = logging.getLogger("artdrop.secrets")

SERVICE = "ArtDrop"


def load(platform_id: str) -> dict:
    """The saved config for one site; {} when there is none (or no keyring)."""
    try:
        raw = keyring.get_password(SERVICE, platform_id)
    except Exception as exc:  # noqa: BLE001 — no usable backend; the app still runs
        log.warning("keyring unavailable: %s", exc)
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("stored config for %s is not valid JSON", platform_id)
        return {}
    return data if isinstance(data, dict) else {}


def save(platform_id: str, config: dict) -> None:
    keyring.set_password(SERVICE, platform_id, json.dumps(config, ensure_ascii=False))


def delete(platform_id: str) -> None:
    try:
        keyring.delete_password(SERVICE, platform_id)
    except Exception:  # noqa: BLE001 — nothing to delete, or no backend
        pass
