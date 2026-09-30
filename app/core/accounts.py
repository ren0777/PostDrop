"""Multiple accounts for one site (Instagram for now).

The shared settings (app id/secret, image-host key) live under the site's own
keyring entry; every account is its own entry "<site>:<key>", and the list of
keys lives in the shared entry so no single entry grows past Credential
Manager's small blob limit.
"""
from __future__ import annotations

import re

from . import secrets

INDEX_KEY = "accounts"
LEGACY_FIELDS = ("tokens", "username")

# The one-time migration of an already-connected single Instagram account
# (the version before multiple accounts): it becomes the account named "Main".
# Nothing is invited anywhere by default — the user fills collaborators in.
LEGACY_NAME = "Main"
LEGACY_CAN_INVITE = False
LEGACY_COLLABORATORS = ""


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return slug or "account"


def card_id(site: str, key: str) -> str:
    return f"{site}:{key}"


def migrate(site: str, cfg: dict) -> dict:
    """Turn a legacy single-account config into one named account, once."""
    if not cfg or cfg.get(INDEX_KEY):
        return cfg
    if not (cfg.get("tokens") or cfg.get("username")):
        return cfg
    key = slugify(LEGACY_NAME)
    try:
        secrets.save(card_id(site, key), {
            "name": LEGACY_NAME,
            "username": str(cfg.get("username") or ""),
            "tokens": cfg.get("tokens") or {},
            "can_invite": LEGACY_CAN_INVITE,
            "collaborators": LEGACY_COLLABORATORS,
        })
        shared_cfg = {k: v for k, v in cfg.items() if k not in LEGACY_FIELDS}
        shared_cfg[INDEX_KEY] = [key]
        secrets.save(site, shared_cfg)
        return shared_cfg
    except Exception:  # noqa: BLE001 — no keyring: leave the config untouched
        return cfg


def shared(site: str) -> dict:
    """The shared settings for a site, with legacy data migrated away."""
    return migrate(site, secrets.load(site))


def keys(site: str) -> list[str]:
    raw = shared(site).get(INDEX_KEY)
    return [str(key) for key in raw] if isinstance(raw, list) else []


def load(site: str, key: str) -> dict:
    return secrets.load(card_id(site, key))


def save(site: str, key: str, account: dict) -> None:
    secrets.save(card_id(site, key), account)


def update(site: str, key: str, fields: dict) -> dict:
    account = load(site, key)
    account.update(fields)
    save(site, key, account)
    return account


def create(site: str, name: str) -> str:
    """A new, unconnected account; returns its key."""
    cfg = shared(site)
    existing = keys(site)
    base = slugify(name)
    key = base
    counter = 2
    while key in existing:
        key = f"{base}-{counter}"
        counter += 1
    existing.append(key)
    cfg[INDEX_KEY] = existing
    secrets.save(site, cfg)
    save(site, key, {"name": str(name or "").strip() or base, "can_invite": False})
    return key


def remove(site: str, key: str) -> None:
    cfg = shared(site)
    cfg[INDEX_KEY] = [item for item in keys(site) if item != key]
    secrets.save(site, cfg)
    secrets.delete(card_id(site, key))


def merged(site: str, key: str) -> dict:
    """Shared settings plus this account — what a connector expects."""
    cfg = dict(shared(site))
    cfg.update(load(site, key))
    cfg["card_id"] = card_id(site, key)
    return cfg


def cards(site: str) -> list[dict]:
    """One entry per account, for the API payloads and the UI.

    The account's own (non-secret) fields are included, so per-site settings
    like `full_auto` travel with it; tokens never do.
    """
    out = []
    for key in keys(site):
        account = load(site, key)
        tokens = account.get("tokens") if isinstance(account.get("tokens"), dict) else {}
        fields = {name: value for name, value in account.items() if name != "tokens"}
        out.append({
            **fields,
            "key": key,
            "id": card_id(site, key),
            "name": str(account.get("name") or key),
            "username": str(account.get("username") or ""),
            "connected": bool(tokens.get("access_token")) or bool(account.get("connected")),
            "can_invite": bool(account.get("can_invite")),
            "collaborators": str(account.get("collaborators") or ""),
            "full_auto": bool(account.get("full_auto")),
        })
    return out
