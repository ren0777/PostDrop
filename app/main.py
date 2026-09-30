"""ArtDrop — FastAPI app: web UI, accounts, uploads, publishing, history."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
import webbrowser
from contextlib import asynccontextmanager
from html import escape as html_escape
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from .core import (accounts, applog, browser, captioner, history, lazy, oauth, paths, queue,
                   secrets, settings)
from .core.images import MAX_INPUT_BYTES
from .core.post import Post, parse_tags
from .platforms import REGISTRY
from .platforms.base import Field, scrub
from .platforms.instagram import parse_collaborators
from .platforms.oauth import OAuthConnector
from .watch import LazyWorker, mixed_names

log = logging.getLogger("artdrop")

POST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
MAX_UPLOAD_FILES = 20
SAFE_NAME_RE = re.compile(r"[^\w.\- ]+")

CAPTION_KEY = "caption"


class _CaptionAccount:
    """Stand-in so `scrub` can hide the Caption AI key in error text."""

    fields = [Field("api_key", "API key", secret=True)]

# One lock per post_id keeps status.json reads and writes consistent, and one
# lock per (post_id, platform) serializes publishes of the same site.
_PUBLISH_LOCKS: dict[str, asyncio.Lock] = {}
_LAZY_WORKER: LazyWorker | None = None  # the folder watcher (M6)


def _lock_for(key: str) -> asyncio.Lock:
    lock = _PUBLISH_LOCKS.get(key)
    if lock is None:
        if len(_PUBLISH_LOCKS) > 512:  # keep the table from growing forever
            for stale_key, stale_lock in list(_PUBLISH_LOCKS.items()):
                if not stale_lock.locked():
                    del _PUBLISH_LOCKS[stale_key]
        lock = asyncio.Lock()
        _PUBLISH_LOCKS[key] = lock
    return lock


def _open_browser_when_ready() -> None:
    import socket

    for _ in range(100):
        try:
            with socket.create_connection((paths.HOST, paths.PORT), timeout=0.25):
                break
        except OSError:
            time.sleep(0.1)
    webbrowser.open(paths.URL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _LAZY_WORKER
    paths.ensure_dirs()
    log_path = applog.setup_logging(paths.DATA)
    log.info("ArtDrop starting — log file: %s", log_path)
    queue.init()
    lazy.ensure_folders()
    _LAZY_WORKER = LazyWorker(publish_card, cards=lambda: {card["id"] for card in _card_labels()})
    _LAZY_WORKER.start()
    removed = paths.clean_old_uploads()
    if removed:
        log.info("removed %s upload folder(s) older than %s days",
                 removed, paths.UPLOAD_RETENTION_DAYS)
    if os.environ.get("ARTDROP_OPEN_BROWSER") == "1":
        threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    yield
    if _LAZY_WORKER is not None:
        await _LAZY_WORKER.stop()
        _LAZY_WORKER = None
    # Close any Pixiv window and stop its Playwright (all on the browser thread).
    try:
        await asyncio.wait_for(browser.run(browser.close_all), timeout=10)
    except Exception:  # noqa: BLE001 — a busy browser must not delay shutdown
        log.warning("the Pixiv window did not close in time; close it by hand")


app = FastAPI(title="ArtDrop", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(paths.WEB)), name="static")


@app.middleware("http")
async def guard(request: Request, call_next):
    """Localhost-only API, plus a custom header on writes (CSRF defense)."""
    if request.url.path.startswith("/api/"):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        if request.method != "GET" and request.headers.get("x-artdrop") != "1":
            return JSONResponse({"error": "missing X-ArtDrop header"}, status_code=403)
    return await call_next(request)


# --------------------------------------------------------------------- pages
@app.get("/")
async def index():
    return FileResponse(paths.WEB / "index.html")


# ------------------------------------------------------------------- status
def _status_file(folder: Path) -> Path:
    return folder / "status.json"


def _read_status(folder: Path) -> dict:
    try:
        data = json.loads(_status_file(folder).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_status(folder: Path, status: dict) -> None:
    _status_file(folder).write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _record_status(folder: Path, platform_id: str, entry: dict) -> None:
    """Merge one site's result into status.json.

    Reads the file again first: parallel publishes each add their own key, so
    writing a cached dict would drop the other sites' results. Call while
    holding the post lock.
    """
    status = _read_status(folder)
    status[platform_id] = entry
    _write_status(folder, status)


def _upload_images(post_id: str) -> list[Path]:
    folder = paths.upload_dir(post_id)
    return sorted(
        path for path in folder.glob("*")
        if path.is_file() and path.name[:2].isdigit()
    )


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "expected a JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")
    return body


def _save_config(key: str, cfg: dict) -> None:
    """Save to the OS credential store, with a clean error when it is unavailable."""
    try:
        secrets.save(key, cfg)
    except Exception as exc:  # noqa: BLE001 — keyring/DPAPI problems are not fatal
        log.exception("could not save the %s config", key)
        raise HTTPException(
            500, "Could not save to the credential store (Windows Credential Manager)."
        ) from exc


# ----------------------------------------------------------------- platforms
MULTI_ACCOUNT_SITES = ("instagram", "pixiv")
# How an account is connected: through OAuth, or by logging in a browser.
ACCOUNT_FLOWS = {"instagram": "oauth", "pixiv": "browser"}
# Which fields an account row may save, and how each one is coerced.
ACCOUNT_FIELDS: dict[str, dict[str, str]] = {
    "instagram": {"name": "text", "can_invite": "bool", "collaborators": "tags"},
    "pixiv": {"name": "text", "full_auto": "bool", "debug_log": "bool"},
}


def _account_settings(site: str, account: dict) -> list[dict]:
    """The extra inputs an account row shows, as payload field dicts."""
    if site == "instagram":
        return [
            {"key": "can_invite", "label": "Can send collab invites", "kind": "checkbox",
             "value": bool(account.get("can_invite")), "help": "", "choices": [], "sticky": False},
            {"key": "collaborators", "label": "Default collaborators (up to 3)", "kind": "text",
             "value": str(account.get("collaborators") or ""), "help": "", "choices": [],
             "sticky": True},
        ]
    if site == "pixiv":
        return [
            {"key": "full_auto", "label": "Full auto — press Post for me",
             "kind": "checkbox", "value": bool(account.get("full_auto")),
             "help": "Off by default. Even then Pixiv gets at most one post every 3 minutes.",
             "choices": [], "sticky": False},
            {"key": "debug_log", "label": "Log Pixiv page details (for troubleshooting)",
             "kind": "checkbox",
             "value": True if account.get("debug_log") is None else bool(account.get("debug_log")),
             "help": "Writes each Pixiv page call (method, path and JSON key names) to "
                     "data\\artdrop.log while a post is in progress. Never passwords or tokens. "
                     "Switch it off once link detection is confirmed.",
             "choices": [], "sticky": False},
        ]
    return []


def _field_payload(field, cfg: dict) -> dict:
    return {
        "key": field.key,
        "label": field.label,
        "secret": field.secret,
        "placeholder": field.placeholder,
        "help": field.help,
        "required": field.required,
        "value": "" if field.secret else str(cfg.get(field.key, "")),
        "saved": bool(field.secret and cfg.get(field.key)),
    }


def _option_fields_payload(connector, extra: list[dict] | None = None) -> list[dict]:
    fields = [
        {
            "key": field.key,
            "label": field.label,
            "kind": field.kind,
            "placeholder": field.placeholder,
            "help": field.help,
            "choices": list(field.choices),
            "sticky": field.sticky,
            "value": "",
        }
        for field in connector.option_fields
    ]
    return fields + list(extra or [])


def _card_entries() -> list[dict]:
    """Every publishable card: one per site, one per account for Instagram."""
    entries = []
    for platform_id, connector in REGISTRY.items():
        if platform_id in MULTI_ACCOUNT_SITES:
            for account in accounts.cards(platform_id):
                entries.append({"id": account["id"], "connector": connector, "account": account})
        else:
            entries.append({"id": platform_id, "connector": connector, "account": None})
    return entries


def _resolve_card(card_id: str):
    """The connector and merged settings for a card id like 'instagram:main'."""
    base, _, key = card_id.partition(":")
    connector = REGISTRY.get(base)
    if connector is None:
        return None, {}
    if key and base in MULTI_ACCOUNT_SITES:
        return connector, accounts.merged(base, key)
    return connector, secrets.load(base)


@app.get("/api/platforms")
async def list_platforms():
    items = []
    for entry in _card_entries():
        card_id = entry["id"]
        connector = entry["connector"]
        account = entry["account"]
        _, cfg = _resolve_card(card_id)

        label = connector.label
        option_fields = _option_fields_payload(connector)
        if account is not None:
            label = f"{connector.label} — {account['name']}"
            if account["can_invite"]:
                option_fields.append({
                    "key": "collaborators",
                    "label": "Collaborators (up to 3)",
                    "kind": "text",
                    "placeholder": "usernames, comma separated",
                    "help": "Clear the box to post without a collaborator.",
                    "choices": [],
                    "sticky": True,  # the account default stays visible between posts
                    "value": account["collaborators"],
                })

        items.append({
            "id": card_id,
            "label": label,
            "caption_limit": connector.caption_limit,
            "max_images": connector.max_images,
            "fields": (
                [] if account is not None
                else [_field_payload(field, cfg) for field in connector.fields]
            ),
            "setup_help": connector.setup_help,
            "configured": not connector.missing(cfg),
            "tags_in_caption": connector.tags_in_caption,
            "is_oauth": connector.is_oauth,
            "connected": connector.connected(cfg),
            "oauth_help": connector.oauth_help if isinstance(connector, OAuthConnector) else "",
            "redirect_default": (
                connector.redirect_default() if isinstance(connector, OAuthConnector) else ""
            ),
            "option_fields": option_fields,
            "account_of": connector.id if account is not None else "",
            "account_key": account["key"] if account is not None else "",
        })

    sections = {}
    for site in MULTI_ACCOUNT_SITES:
        connector = REGISTRY[site]
        cfg = accounts.shared(site)
        sections[site] = {
            "id": site,
            "label": f"{connector.label} settings",
            "flow": ACCOUNT_FLOWS.get(site, "oauth"),
            "configured": not connector.missing(cfg),
            "fields": [_field_payload(field, cfg) for field in connector.fields],
            "setup_help": connector.setup_help,
            "oauth_help": connector.oauth_help if isinstance(connector, OAuthConnector) else "",
            "accounts": [
                {**account, "settings": _account_settings(site, account)}
                for account in accounts.cards(site)
            ],
        }
    return {"platforms": items, "account_sections": sections}


@app.get("/api/settings")
async def get_settings():
    return {"caption_styles": _caption_styles()}


@app.post("/api/settings")
async def save_settings(request: Request):
    body = await _json_body(request)
    styles = body.get("caption_styles")
    known = {entry["id"] for entry in _card_entries()}
    if isinstance(styles, dict):
        settings.set_styles({key: str(value) for key, value in styles.items() if key in known})
    return {"ok": True, "caption_styles": _caption_styles()}


def _caption_styles() -> dict:
    return {
        entry["id"]: settings.style_for(entry["id"], entry["connector"].default_caption_style)
        for entry in _card_entries()
    }


@app.post("/api/accounts/{platform_id}")
async def save_account(platform_id: str, request: Request):
    connector = REGISTRY.get(platform_id)
    if connector is None:
        raise HTTPException(404, "unknown platform")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "expected a JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")

    cfg = secrets.load(platform_id)
    for field in connector.fields:
        if field.key not in body:
            continue
        value = str(body.get(field.key) or "").strip()
        if field.secret and not value:
            continue  # an empty secret means "keep the saved value"
        cfg[field.key] = value
    _save_config(platform_id, cfg)
    return {"ok": True, "configured": not connector.missing(cfg),
            "warning": connector.config_warning(cfg)}


@app.delete("/api/accounts/{platform_id}")
async def remove_account(platform_id: str):
    if platform_id not in REGISTRY:
        raise HTTPException(404, "unknown platform")
    secrets.delete(platform_id)
    return {"ok": True}


# ------------------------------------------------------ account-managed rows
@app.post("/api/{site}/accounts")
async def add_account_row(site: str, request: Request):
    if site not in MULTI_ACCOUNT_SITES:
        raise HTTPException(404, "unknown site")
    body = await _json_body(request)
    name = str(body.get("name") or "").strip() or REGISTRY[site].label
    key = accounts.create(site, name)
    return {"ok": True, "key": key, "id": accounts.card_id(site, key)}


@app.post("/api/{site}/accounts/{key}")
async def save_account_row(site: str, key: str, request: Request):
    if site not in MULTI_ACCOUNT_SITES or key not in accounts.keys(site):
        raise HTTPException(404, "unknown account")
    body = await _json_body(request)
    updates = {}
    for field_key, kind in ACCOUNT_FIELDS.get(site, {}).items():
        if field_key not in body:
            continue
        value = body.get(field_key)
        if kind == "bool":
            updates[field_key] = bool(value)
        elif kind == "tags":
            updates[field_key] = ",".join(parse_collaborators(value))
        else:
            updates[field_key] = str(value or "").strip()
    accounts.update(site, key, updates)
    return {"ok": True}


@app.delete("/api/{site}/accounts/{key}")
async def remove_account_row(site: str, key: str):
    if site not in MULTI_ACCOUNT_SITES or key not in accounts.keys(site):
        raise HTTPException(404, "unknown account")
    accounts.remove(site, key)
    return {"ok": True}


# ------------------------------------------------- browser-driven accounts
@app.post("/api/browser-login/{platform_id}")
async def browser_login(platform_id: str):
    connector, cfg = _resolve_card(platform_id)
    if connector is None:
        raise HTTPException(404, "unknown platform")
    try:
        detail = await connector.login(cfg)
    except Exception as exc:  # noqa: BLE001 — report, don't 500
        log.exception("browser login failed for %s", platform_id)
        return {"ok": False, "error": scrub(f"{type(exc).__name__}: {exc}", cfg, connector)}
    return {"ok": True, "detail": detail}


@app.post("/api/browser-logout/{platform_id}")
async def browser_logout(platform_id: str):
    connector, cfg = _resolve_card(platform_id)
    if connector is None:
        raise HTTPException(404, "unknown platform")
    try:
        detail = await connector.logout(cfg)
    except Exception as exc:  # noqa: BLE001
        log.exception("browser logout failed for %s", platform_id)
        return {"ok": False, "error": scrub(f"{type(exc).__name__}: {exc}", cfg, connector)}
    return {"ok": True, "detail": detail}


@app.post("/api/test/{platform_id}")
async def test_account(platform_id: str):
    connector, cfg = _resolve_card(platform_id)
    if connector is None:
        raise HTTPException(404, "unknown platform")
    missing = connector.missing(cfg)
    if missing:
        return {"ok": False, "error": "Not set up yet — missing: " + ", ".join(missing)}
    try:
        detail = await connector.test(cfg)
    except Exception as exc:  # noqa: BLE001 — report, don't 500
        log.exception("test failed for %s", platform_id)
        return {"ok": False, "error": scrub(f"{type(exc).__name__}: {exc}", cfg, connector)}
    return {"ok": True, "detail": detail}


# ------------------------------------------------------------ upload/publish
@app.post("/api/upload")
async def upload(images: list[UploadFile] = File(...)):
    if not images:
        raise HTTPException(400, "no files were uploaded")
    if len(images) > MAX_UPLOAD_FILES:
        raise HTTPException(400, f"at most {MAX_UPLOAD_FILES} files per post")

    post_id = uuid.uuid4().hex
    folder = paths.upload_dir(post_id)
    folder.mkdir(parents=True, exist_ok=True)

    saved = []
    for index, item in enumerate(images, start=1):
        data = await item.read()
        if not data:
            continue
        if len(data) > MAX_INPUT_BYTES:
            raise HTTPException(
                400,
                f"{item.filename} is larger than {MAX_INPUT_BYTES // (1024 * 1024)} MB "
                "— resize or export it smaller first",
            )
        try:
            with Image.open(BytesIO(data)) as probe:
                probe.verify()
        except Exception:
            raise HTTPException(400, f"{item.filename} is not a valid image")

        name = Path(item.filename or "image").name
        name = SAFE_NAME_RE.sub("_", name)[:80] or "image"
        (folder / f"{index:02d}_{name}").write_bytes(data)
        saved.append({"name": name, "kb": max(1, round(len(data) / 1024))})

    if not saved:
        raise HTTPException(400, "no images were uploaded")
    return {"post_id": post_id, "images": saved}


@app.post("/api/publish")
async def publish(request: Request):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "expected a JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")

    platform_id = str(body.get("platform") or "")
    connector, cfg = _resolve_card(platform_id)
    if connector is None:
        raise HTTPException(404, "unknown platform")

    post_id = str(body.get("post_id") or "")
    if not POST_ID_RE.match(post_id):
        raise HTTPException(400, "bad post id")
    image_paths = _upload_images(post_id)
    if not image_paths:
        raise HTTPException(400, "no images for this post")

    post = Post(
        images=image_paths,
        caption=str(body.get("caption") or ""),
        tags=parse_tags(body.get("tags")),
        alt_text=str(body.get("alt_text") or ""),
        options=body.get("options") if isinstance(body.get("options"), dict) else {},
        character=str(body.get("character") or ""),
        series=str(body.get("series") or ""),
    )
    return await publish_card(platform_id, post, post_id, bool(body.get("dry_run")))


async def publish_card(platform_id: str, post: Post, post_id: str, dry_run: bool) -> dict:
    """The one publish path — the Post tab and the lazy queue both use it.

    The whole publish is serialized per site, so a double click can never post
    twice; the status check and the record share a lock per post_id, so sites
    publishing in parallel can't overwrite each other's status.json entries.
    """
    connector, cfg = _resolve_card(platform_id)
    if connector is None:
        return {"ok": False, "error": "unknown platform"}
    folder = paths.upload_dir(post_id)

    if dry_run:
        result = await connector.publish(post, cfg, True)
        history.record(
            post_id=post_id, platform=platform_id, ok=result.ok, dry_run=True,
            url=result.url, error=result.error, caption=post.caption, note=result.note,
        )
        return {"ok": result.ok, "url": result.url, "error": result.error,
                "preview": result.preview, "note": result.note}

    async with _lock_for(f"{post_id}:{platform_id}"):
        # Double-post guard: a site that already succeeded for this post_id is final.
        async with _lock_for(post_id):
            done = _read_status(folder).get(platform_id) or {}
            if done.get("ok"):
                return {"ok": True, "url": done.get("url"), "already": True}

        result = await connector.publish(post, cfg, False)

        async with _lock_for(post_id):
            _record_status(folder, platform_id, {
                "ok": result.ok,
                "url": result.url,
                "error": result.error,
                "ts": time.time(),
            })

    history.record(
        post_id=post_id, platform=platform_id, ok=result.ok, dry_run=False,
        url=result.url, error=result.error, caption=post.caption, note=result.note,
    )
    return {"ok": result.ok, "url": result.url, "error": result.error,
            "preview": result.preview, "note": result.note}


# ---------------------------------------------------------- lazy mode (M6)
def _card_labels() -> list[dict]:
    out = []
    for entry in _card_entries():
        account = entry.get("account") or {}
        label = entry["connector"].label
        if account:
            label = f"{label} — {account.get('name') or account.get('key')}"
        out.append({"id": entry["id"], "label": label, "site": entry["connector"].id})
    return out


def lazy_status() -> dict:
    """What the Queue tab needs: settings, folders, cards and the counters."""
    data = lazy.stored()
    existing = {card["id"] for card in _card_labels()}
    folders = []
    for folder in data["folders"]:
        path = lazy.resolve(folder["path"])
        folders.append({**folder, "resolved": str(path), "exists": path.exists(),
                        "cards": [card for card in folder["cards"] if card in existing]})
    return {
        "settings": {**data, "folders": folders},
        "root": str(lazy.root()),
        "cards": _card_labels(),
        "sites": lazy.SITE_LIMITS,
        "min_spacing_minutes": lazy.MIN_SPACING_MINUTES,
        "working": bool(_LAZY_WORKER and not _LAZY_WORKER.last_error),
        "last_error": (_LAZY_WORKER.last_error if _LAZY_WORKER else ""),
    }


@app.get("/api/lazy")
async def get_lazy():
    return lazy_status()


@app.post("/api/lazy")
async def save_lazy(request: Request):
    body = await _json_body(request)
    existing = {card["id"] for card in _card_labels()}
    if isinstance(body.get("folders"), list):
        for folder in body["folders"]:
            if not isinstance(folder, dict):
                continue
            unknown = [card for card in (folder.get("cards") or []) if card not in existing]
            if unknown:
                return {"ok": False, "error": "unknown card: " + ", ".join(unknown)}
    lazy.save(body)
    lazy.ensure_folders()
    return {"ok": True, **lazy_status()}


@app.post("/api/lazy/pause")
async def pause_lazy(request: Request):
    body = await _json_body(request)
    lazy.save({"paused": bool(body.get("paused"))})
    return {"ok": True, **lazy_status()}


@app.post("/api/lazy/scan")
async def scan_lazy():
    if _LAZY_WORKER is None:
        return {"ok": False, "error": "the folder watcher is not running"}
    # Pressing the button is a manual nudge: forget the AI backoff so a blip
    # that has passed is retried right away (the automatic passes still wait).
    queue.ai_forget_prefix("identify:")
    queue.ai_forget_prefix("caption:")
    summary = await _LAZY_WORKER.scan()
    return {"ok": True, "summary": summary, **lazy_status()}


@app.post("/api/lazy/pull-back")
async def pull_back(request: Request):
    """"Wrong? Move to Needs you": take a queued set back before it posts (M6.1)."""
    body = await _json_body(request)
    batch_key = str(body.get("batch_key") or "")
    waiting = [item for item in queue.items(limit=500)
               if item["batch_key"] == batch_key and item["status"] in ("pending", "failed")]
    if not waiting:
        return {"ok": False, "error": "that set has nothing waiting to post"}
    images = sorted({path for item in waiting for path in item["images"]})
    first = waiting[0]
    entry_id = queue.add_needs_you(
        batch_key=batch_key, source=str(first.get("source") or ""),
        folder_key=str(first.get("folder_key") or ""), kind="unknown",
        images=images, cover=str(first.get("cover") or ""),
        caption=str(first.get("caption") or ""), tags=first.get("tags") or [],
        alt_text=str(first.get("alt_text") or ""),
        character=str(first.get("character") or ""), series=str(first.get("series") or ""),
        platforms={}, mature=bool(first.get("mature")),
    )
    for item in waiting:
        queue.skip_item(int(item["id"]))
    _finish_batch_of({"batch_key": batch_key})
    return {"ok": True, "entry": entry_id, "needs_you": _needs_you_payload(),
            "queue": queue.queue_payload()}


def _needs_you_payload() -> list[dict]:
    """The Needs-you entries, plus whether each entry is the set's last blocker.

    A Pixiv entry whose set has nothing pending is only waiting for the artist
    ("waiting for your Pixiv decision" on the Queue tab).
    """
    entries = []
    for entry in queue.needs_you():
        item = dict(entry)
        item["waiting_pixiv"] = (
            str(entry.get("kind") or "") == "pixiv"
            and queue.batch_pending(str(entry.get("batch_key") or "")) == 0
        )
        entries.append(item)
    return entries


@app.get("/api/queue")
async def get_queue():
    data = lazy.stored()
    stuck = []
    for folder in data["folders"]:
        directory = lazy.resolve(folder["path"])
        stuck += [{"folder": folder["key"], "file": name}
                  for name in lazy.unusable(directory)]
    return {
        "queue": queue.queue_payload(),
        "needs_you": _needs_you_payload(),
        "cards": _card_labels(),
        "lazy": lazy_status()["settings"],
        "stuck": stuck,
    }


@app.post("/api/queue/{item_id}/caption")
async def queue_caption(item_id: int, request: Request):
    body = await _json_body(request)
    tags = body.get("tags")
    ok = queue.update_caption(item_id, str(body.get("caption") or ""),
                              parse_tags(tags) if tags is not None else None)
    return {"ok": ok, "queue": queue.queue_payload()}


@app.post("/api/queue/{item_id}/{action}")
async def queue_action(item_id: int, action: str):
    if action == "now":
        entry = queue.item(item_id)
        if entry is None:
            raise HTTPException(404, "unknown item")
        if _LAZY_WORKER is None:
            return {"ok": False, "error": "the folder watcher is not running"}
        result = await _LAZY_WORKER._post(entry, entry["card_id"])
        return {"ok": bool(result["ok"]), "result": result, "queue": queue.queue_payload()}
    if action == "skip":
        entry = queue.item(item_id)
        ok = queue.skip_item(item_id)
        _finish_batch_of(entry)
        queue.compact_schedule()  # the freed slot moves the next posts up
        return {"ok": ok, "queue": queue.queue_payload()}
    if action == "retry":
        return {"ok": queue.retry_item(item_id), "queue": queue.queue_payload()}
    if action == "remove":
        entry = queue.item(item_id)
        ok = queue.remove_item(item_id)
        _finish_batch_of(entry)
        queue.compact_schedule()  # the freed slot moves the next posts up
        return {"ok": ok, "needs_you": _needs_you_payload(), "queue": queue.queue_payload()}
    raise HTTPException(404, "unknown action")


def _finish_batch_of(entry: dict | None) -> None:
    """A skipped/removed item (or a resolved entry) can be the last one: close it."""
    if not entry or _LAZY_WORKER is None:
        return
    _LAZY_WORKER._finish_batch(str(entry.get("batch_key") or ""))


@app.get("/api/queue/{item_id}/thumb")
async def queue_thumb(item_id: int):
    entry = queue.item(item_id)
    if entry is None:
        raise HTTPException(404, "unknown item")
    cover = Path(str(entry.get("cover") or ""))
    if not cover.exists():
        raise HTTPException(404, "no image")
    return FileResponse(cover)


@app.post("/api/needs-you/{entry_id}/use")
async def use_needs_you(entry_id: int):
    """Stage a 'Needs you' batch as a draft for the normal Post tab."""
    entry = queue.needs_you_entry(entry_id)
    if entry is None:
        raise HTTPException(404, "unknown entry")
    post_id, images = _stage_upload([Path(path) for path in entry.get("images") or []])
    if not images:
        return {"ok": False, "error": "the batch's images are gone"}
    queue.resolve_needs_you(entry_id, "used")
    queue.mark_files(entry.get("images") or [], "used")
    _finish_batch_of(entry)
    mixed = mixed_names(entry)
    return {
        "ok": True,
        "post_id": post_id,
        "images": images,
        "kind": entry.get("kind"),
        "caption": entry.get("caption") or "",
        "tags": entry.get("tags") or [],
        "alt_text": entry.get("alt_text") or "",
        # a mixed set's "Mixed: A, B…" label is not a character: the Post tab
        # gets the names separately and keeps the Character box empty
        "character": "" if mixed else entry.get("character") or "",
        "series": "" if mixed else entry.get("series") or "",
        "mixed": mixed,
        "platforms": entry.get("platforms") or {},
        "mature": bool(entry.get("mature")),
        "cards": _folder_cards(str(entry.get("folder_key") or "")),
    }


def _folder_cards(folder_key: str) -> list[str]:
    """The cards a lazy folder routes to, the way the worker resolves them."""
    data = lazy.stored()
    existing = {card["id"] for card in _card_labels()}
    for folder in data["folders"]:
        if str(folder.get("key")) != folder_key:
            continue
        if _LAZY_WORKER is not None:
            return _LAZY_WORKER.routed_cards(list(folder.get("cards") or []), existing)
        return [card for card in folder.get("cards") or [] if card in existing]
    return []


@app.post("/api/needs-you/{entry_id}/dismiss")
async def dismiss_needs_you(entry_id: int):
    entry = queue.needs_you_entry(entry_id)
    ok = queue.resolve_needs_you(entry_id, "dismissed")
    if ok and entry:
        queue.mark_files(entry.get("images") or [], "dismissed")
        _finish_batch_of(entry)
    return {"ok": ok, "needs_you": _needs_you_payload()}


@app.post("/api/needs-you/{entry_id}/retry-ai")
async def retry_needs_you(entry_id: int):
    """Re-run identify/caption for a "Needs you" entry the AI failed on."""
    entry = queue.needs_you_entry(entry_id)
    if entry is None:
        raise HTTPException(404, "unknown entry")
    if not entry.get("note"):
        return {"ok": False, "error": "that entry was not the AI's fault"}
    images = [Path(path) for path in entry.get("images") or []]
    queue.resolve_needs_you(entry_id, "retrying")
    queue.forget_files(images)  # the next scan sees them as new work again
    queue.ai_forget_prefix("identify:")
    queue.ai_forget_prefix("caption:")
    summary = await _LAZY_WORKER.scan() if _LAZY_WORKER is not None else {}
    return {"ok": True, "summary": summary, "needs_you": _needs_you_payload(),
            "queue": queue.queue_payload()}


@app.get("/api/needs-you/{entry_id}/thumb")
async def needs_you_thumb(entry_id: int):
    entry = queue.needs_you_entry(entry_id)
    if entry is None:
        raise HTTPException(404, "unknown entry")
    cover = Path(str(entry.get("cover") or ""))
    if not cover.exists():
        raise HTTPException(404, "no image")
    return FileResponse(cover)


@app.get("/api/posts/{post_id}/image/{index}")
async def post_image(post_id: str, index: int):
    """One staged image, for the thumbnails of a batch opened from Needs you."""
    if not POST_ID_RE.match(post_id):
        raise HTTPException(400, "bad post id")
    images = _upload_images(post_id)
    if index < 0 or index >= len(images):
        raise HTTPException(404, "no such image")
    return FileResponse(images[index])


def _stage_upload(files: list[Path]) -> tuple[str, list[dict]]:
    """Copy files into a fresh upload folder (what /api/upload does for the UI)."""
    post_id = uuid.uuid4().hex
    folder = paths.upload_dir(post_id)
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for index, path in enumerate(files, start=1):
        if not path.exists():
            continue
        name = path.name[:70]
        (folder / f"{index:02d}_{name}").write_bytes(path.read_bytes())
        saved.append({"name": name})
    return post_id, saved


@app.get("/api/history")
async def get_history(limit: int = 100):
    return {"items": history.recent(max(1, min(int(limit), 500)))}


# ---------------------------------------------------------------- caption AI
@app.get("/api/caption/settings")
async def caption_settings():
    cfg = secrets.load(CAPTION_KEY)
    return {
        "api_key_saved": bool(cfg.get("api_key")),
        "vision_model": cfg.get("vision_model") or captioner.DEFAULT_VISION_MODEL,
        "backup_model": cfg.get("backup_model") or captioner.DEFAULT_BACKUP_MODEL,
        "strong_model": cfg.get("strong_model") or "",
        "add_ai_tag": bool(cfg.get("add_ai_tag", True)),
        "configured": bool(cfg.get("api_key")),
    }


@app.post("/api/caption/settings")
async def save_caption_settings(request: Request):
    body = await _json_body(request)
    cfg = secrets.load(CAPTION_KEY)
    if body.get("api_key"):
        cfg["api_key"] = str(body["api_key"]).strip()
    for key in ("vision_model", "backup_model"):
        value = str(body.get(key) or "").strip()
        if value:
            cfg[key] = value
    if "strong_model" in body:  # optional third model, for hard-to-recognise characters
        cfg["strong_model"] = str(body.get("strong_model") or "").strip()
    if "add_ai_tag" in body:
        cfg["add_ai_tag"] = bool(body["add_ai_tag"])
    _save_config(CAPTION_KEY, cfg)
    return {"ok": True, "configured": bool(cfg.get("api_key"))}


@app.delete("/api/caption/settings")
async def remove_caption_settings():
    secrets.delete(CAPTION_KEY)
    return {"ok": True}


@app.post("/api/caption/test")
async def test_caption_settings():
    cfg = secrets.load(CAPTION_KEY)
    try:
        detail = await captioner.test_connection(cfg)
    except Exception as exc:  # noqa: BLE001 — report, don't 500
        log.exception("caption AI test failed")
        return {"ok": False, "error": scrub(str(exc), cfg, _CaptionAccount())}
    return {"ok": True, "detail": detail}


@app.post("/api/caption")
async def make_caption(request: Request):
    body = await _json_body(request)
    # Cards, not sites: two Instagram accounts each get their own entry, and
    # accounts of one site can share a single AI answer in the prompt.
    card_sites = {entry["id"]: entry["connector"].id for entry in _card_entries()}
    chosen = [str(pid) for pid in (body.get("platforms") or []) if str(pid) in card_sites]
    sites: list[str] = []
    for card_id in chosen:
        if card_sites[card_id] not in sites:
            sites.append(card_sites[card_id])

    post_id = str(body.get("post_id") or "")
    images = []
    if post_id:
        if not POST_ID_RE.match(post_id):
            raise HTTPException(400, "bad post id")
        images = _upload_images(post_id)

    cfg = secrets.load(CAPTION_KEY)
    mixed = body.get("mixed") if isinstance(body.get("mixed"), dict) else None
    try:
        if mixed is not None:
            # a mixed set from the queue: one universal caption, every name tagged
            result = await captioner.generate_mixed(
                cfg=cfg, images=images,
                characters=[str(name) for name in mixed.get("characters") or []],
                series=[str(name) for name in mixed.get("series") or []],
                platforms=sites, mature=bool(body.get("mature")),
                caption_draft=str(body.get("caption") or ""),
                caption_exact=bool(body.get("caption_exact")),
            )
        else:
            result = await captioner.generate(
                cfg=cfg,
                images=images,
                character=str(body.get("character") or ""),
                series=str(body.get("series") or ""),
                notes=str(body.get("notes") or ""),
                mature=bool(body.get("mature")),
                platforms=sites,
                caption_draft=str(body.get("caption") or ""),
                caption_exact=bool(body.get("caption_exact")),
            )
    except captioner.CaptionError as exc:
        return {"ok": False, "error": scrub(str(exc), cfg, _CaptionAccount())}
    except Exception as exc:  # noqa: BLE001 — report, don't 500
        log.exception("caption generation failed")
        return {"ok": False, "error": scrub(f"{type(exc).__name__}: {exc}", cfg, _CaptionAccount())}

    # Key the answer by card id so the UI fills the right card.
    answers = result.get("platforms") or {}
    result["platforms"] = {
        card_id: answers.get(card_sites[card_id]) or {"caption": "", "tags": []}
        for card_id in chosen
    }
    return {"ok": True, **result}


# -------------------------------------------------------------------- OAuth
def _oauth_connector(platform_id: str) -> OAuthConnector:
    connector, _ = _resolve_card(platform_id)
    if not isinstance(connector, OAuthConnector):
        raise HTTPException(404, "not an OAuth platform")
    return connector


def _oauth_page(title: str, message: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'><title>ArtDrop</title>"
        "<style>body{font-family:'Segoe UI',system-ui,sans-serif;background:#12121a;color:#e8e8f0;"
        "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
        "div{max-width:560px;padding:32px;background:#1b1b26;border:1px solid #2e2e3d;"
        "border-radius:14px;text-align:center}h1{font-size:20px;margin:0 0 10px}"
        "p{color:#9a9ab0;line-height:1.5}</style></head>"
        f"<body><div><h1>{html_escape(title)}</h1><p>{html_escape(message)}</p></div></body></html>"
    )


@app.post("/api/oauth/{platform_id}/start")
async def oauth_start(platform_id: str):
    connector = _oauth_connector(platform_id)
    _, cfg = _resolve_card(platform_id)
    missing = connector.missing(cfg)
    if missing:
        return {"ok": False, "error": "Fill in and save these first: " + ", ".join(missing)}
    try:
        url = connector.start_url(cfg)
    except Exception as exc:  # noqa: BLE001 — report, don't 500
        log.exception("oauth start failed for %s", platform_id)
        return {"ok": False, "error": scrub(f"{type(exc).__name__}: {exc}", cfg, connector)}
    return {"ok": True, "url": url, "redirect_uri": connector.redirect_uri(cfg)}


@app.post("/api/oauth/{platform_id}/code")
async def oauth_code(platform_id: str, request: Request):
    connector = _oauth_connector(platform_id)
    body = await _json_body(request)
    _, cfg = _resolve_card(platform_id)
    missing = connector.missing(cfg)
    if missing:
        return {"ok": False, "error": "Fill in and save these first: " + ", ".join(missing)}

    text = str(body.get("code") or "").strip()
    code, state = text, ""
    if "code=" in text:  # the whole redirected URL is fine too
        query = parse_qs(urlparse(text).query)
        code = str((query.get("code") or [""])[0])
        state = str((query.get("state") or [""])[0])
    if not code:
        return {"ok": False, "error": "No code was given."}

    # PKCE: the verifier comes from the pasted state, or the newest one we made.
    entry = oauth.take_state(state, platform_id) if state else None
    verifier = (entry or {}).get("verifier") or oauth.latest_verifier(platform_id)
    if connector.oauth_provider.pkce and not verifier:
        return {"ok": False,
                "error": "Press Connect again, then paste the new address from the browser."}

    try:
        await connector.complete(cfg, code, code_verifier=verifier or None)
    except Exception as exc:  # noqa: BLE001 — report, don't 500
        log.exception("oauth code exchange failed for %s", platform_id)
        return {"ok": False, "error": scrub(f"{type(exc).__name__}: {exc}", cfg, connector)}
    return {"ok": True}


@app.post("/api/oauth/{platform_id}/disconnect")
async def oauth_disconnect(platform_id: str):
    _oauth_connector(platform_id)
    base, _, key = platform_id.partition(":")
    if key and base in MULTI_ACCOUNT_SITES:
        accounts.update(base, key, {"tokens": {}})
    else:
        cfg = secrets.load(platform_id)
        cfg.pop("tokens", None)
        _save_config(platform_id, cfg)
    return {"ok": True}


@app.get("/oauth/{platform_id}")
async def oauth_callback(platform_id: str, request: Request):
    """Where the provider sends the browser back to (no headers required)."""
    connector = _oauth_connector(platform_id)
    params = request.query_params
    if params.get("error"):
        return _oauth_page(
            f"{connector.label}: not connected",
            params.get("error_description") or params.get("error") or "access was denied",
        )
    code = params.get("code") or ""
    if not code:
        return _oauth_page("ArtDrop", "No code came back in the redirect.")
    entry = oauth.take_state(params.get("state") or "", platform_id)
    if entry is None:
        return _oauth_page(
            f"{connector.label}: not connected",
            "The state value did not match (it may have expired). Press Connect again.",
        )
    _, cfg = _resolve_card(platform_id)
    try:
        await connector.complete(cfg, code, code_verifier=entry.get("verifier") or None)
    except Exception as exc:  # noqa: BLE001 — show the provider's reason
        log.exception("oauth callback failed for %s", platform_id)
        return _oauth_page(f"{connector.label}: not connected", scrub(str(exc), cfg, connector))
    return _oauth_page(
        f"{connector.label}: connected ✓",
        "You can close this tab and return to ArtDrop, then press Test connection.",
    )
