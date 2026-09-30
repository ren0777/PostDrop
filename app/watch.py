"""The lazy-mode worker (M6): watch the folders, caption batches, post the queue.

It runs as one background task while ArtDrop is open. Every pass:

1. **scans** the outbox folders for settled batches and turns each new one into
   queue items — one AI call per batch, on the cover image, identifies the
   character and writes the captions; chunks per site are spaced out per card.
2. **posts** whatever is due, one item at a time, through the same publish path
   the Post tab uses (`publish` is injected by the app), then moves a finished
   batch's files to `posted\\<date>\\` with a small `links.json`.

Pixiv is never posted here: every batch also gets a **Needs you: Pixiv** entry,
because a human picks the rating and presses Post.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from .core import captioner, lazy, paths, queue, secrets
from .core.post import Post, parse_tags

log = logging.getLogger("artdrop.lazy")

CAPTION_KEY = "caption"
PIXIV_SITE = "pixiv"
IDENTIFY_LIMIT = 50  # AI look-ups per scan pass (cost guard)
IDENTIFY_PARALLEL = 3  # requests in flight at once
ESCALATION_LIMIT = 12  # identify calls per pass that may also ask the backup/strong model
AI_UNAVAILABLE = "AI unavailable"
AI_ERROR = "AI error"
UNIVERSAL_LABEL = "Universal"
ORIGINAL_NOTE = "looks like an original character"
DISAGREE_NOTE = "two models named different characters — check the name"
DISAGREE_CAPTION = "AI disagreed about the character"
MISSING_NOTE = ("files missing — the image was moved or deleted, so this post is on hold. "
                "Put the file back, or Remove this post.")
MOVED_NOTE = "moved to a newer set"


class LazyWorker:
    """The folder watcher and queue runner. One instance per app run."""

    def __init__(self, publish, cards=None, caption=None, identify=None, mixed=None,
                 interval: float = 15.0):
        self.publish = publish  # async (card_id, post, post_id, dry_run) -> dict
        self._cards = cards or (lambda: set())
        self.caption = caption or captioner.generate_lazy
        self.identify = identify or captioner.identify
        self.mixed = mixed or captioner.generate_mixed
        self.interval = float(interval)
        self.last_error = ""
        self.identify_calls = 0  # this pass, reset by `scan`
        self.escalations = 0  # this pass: identifies allowed to use the backup models
        self.recovered = False  # the one-off retry of older AI-error entries
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutting down
            pass
        self._task = None

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad pass must not kill the loop
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("lazy pass failed")
            await asyncio.sleep(self.interval)

    async def tick(self) -> dict:
        scanned = await self.scan()
        posted = await self.post_due()
        return {"scan": scanned, "posted": posted}

    # ------------------------------------------------------------------- cards
    def card_ids(self) -> set[str]:
        try:
            return {str(card_id) for card_id in self._cards()}
        except Exception:  # noqa: BLE001 — no cards yet
            return set()

    def routed_cards(self, wanted: list[str], existing: set[str]) -> list[str]:
        """The folder's cards that really exist (a renamed account falls back)."""
        cards: list[str] = []
        for card_id in wanted:
            if card_id in existing:
                cards.append(card_id)
                continue
            site = card_id.partition(":")[0]
            if ":" in card_id and site == "instagram":
                fallback = sorted(item for item in existing if item.startswith("instagram:"))
                if fallback and fallback[0] not in cards:
                    cards.append(fallback[0])
        return cards

    # ---------------------------------------------------------------- scanning
    async def scan(self) -> dict:
        data = lazy.stored()
        if not data["enabled"]:
            return {"queued": 0, "needs_you": 0, "reason": "off"}
        if data["paused"]:
            self.reconcile()  # housekeeping only: the Queue tab stays truthful while paused
            return {"queued": 0, "needs_you": 0, "reason": "paused"}

        existing = self.card_ids()
        known = queue.known_files()
        digests: dict[Path, str] = {}
        created = 0
        waiting = 0
        self.identify_calls = 0
        self.escalations = 0
        if not self.recovered:
            self.recovered = True
            self._recover_ai_entries()
            queue.compact_schedule()  # once per start: close gaps left while it was off
        for folder in data["folders"]:
            directory = lazy.resolve(folder["path"])
            cards = self.routed_cards(folder["cards"], existing)
            mature = bool(folder["mature"])
            for batch in lazy.batches(directory):
                # A file counts as already handled only while its content matches
                # what was recorded for that path: a new file with an old name
                # (1.png again) is new, and a changed file is new too.
                fresh = [path for path in batch
                         if not self._known(known, path, digests)]
                if not fresh:
                    continue
                if batch[0].parent == directory:
                    # Loose files: they may mix characters, so each image is asked
                    # about separately and the answers become the sets (M6.1).
                    result = await self._split_loose(directory, folder, cards, fresh,
                                                     digests, mature=mature)
                else:
                    # A subfolder is an explicit set: never split, but still
                    # identified — a mixed set gets a universal caption (M6.1).
                    result = await self._explicit_set(directory, folder, cards, batch, fresh,
                                                      digests, mature=mature)
                created += result["queued"]
                waiting += result["needs_you"]
        # After the new sets claimed their files: a file that moved into one of
        # them leaves its old set, and a vanished file puts its post on hold.
        self.reconcile()
        return {"queued": created, "needs_you": waiting}

    def reconcile(self) -> dict:
        """Check every waiting post's files; never post one whose images are gone.

        No hashing: a missing path's content is known from the `files` table, and
        the same content recorded at another path that exists means a newer set
        owns it now (the artist moved the file there). Per waiting item:
        - moved or vanished images are dropped while others remain;
        - none left and every image moved → "superseded" (hidden, never posted);
        - none left otherwise → "missing": shown on the Queue tab with Remove,
          and back to "pending" if the files come back to where they were.
        'Needs you' entries get the same treatment (an all-gone one gets a note).
        """
        known = queue.known_files()
        by_digest: dict[str, list[str]] = {}
        for path, record in known.items():
            if record.get("digest"):
                by_digest.setdefault(str(record["digest"]), []).append(path)

        def moved(path: str) -> bool:
            digest = str((known.get(path) or {}).get("digest") or "")
            return bool(digest) and any(
                other != path and Path(other).exists() for other in by_digest.get(digest, []))

        changed = {"dropped": 0, "missing": 0, "superseded": 0, "restored": 0}
        batches: set[str] = set()
        for entry in [*queue.items("pending", limit=1000), *queue.items("failed", limit=1000),
                      *queue.items("missing", limit=1000)]:
            images = [str(path) for path in entry.get("images") or []]
            here = [path for path in images if Path(path).exists()]
            if len(here) == len(images):
                if entry["status"] == "missing" and images:
                    queue.set_item_files(int(entry["id"]), images, status="pending", error="")
                    changed["restored"] += 1
                continue
            if entry["status"] == "missing":
                gone = [path for path in images if path not in here]
                if gone and all(moved(path) for path in gone):
                    queue.set_item_files(int(entry["id"]), images, status="superseded",
                                         error=MOVED_NOTE)
                    changed["superseded"] += 1
                    batches.add(str(entry.get("batch_key") or ""))
                continue  # still waiting for the artist
            if here:
                queue.set_item_files(int(entry["id"]), here)
                changed["dropped"] += len(images) - len(here)
                log.info("lazy item %s: %s image(s) moved or deleted — posting the rest",
                         entry["id"], len(images) - len(here))
            elif images and all(moved(path) for path in images):
                queue.set_item_files(int(entry["id"]), images, status="superseded",
                                     error=MOVED_NOTE)
                changed["superseded"] += 1
                batches.add(str(entry.get("batch_key") or ""))
                log.info("lazy item %s: its images moved to a newer set — dropped", entry["id"])
            else:
                queue.set_item_files(int(entry["id"]), images, status="missing",
                                     error=MISSING_NOTE)
                changed["missing"] += 1
                log.warning("lazy item %s: its files are missing — on hold", entry["id"])

        for entry in queue.needs_you():
            images = [str(path) for path in entry.get("images") or []]
            here = [path for path in images if Path(path).exists()]
            if not images or len(here) == len(images):
                continue
            if here:
                queue.set_needs_you_files(int(entry["id"]), here)
            elif all(moved(path) for path in images):
                queue.set_needs_you_files(int(entry["id"]), images, status="superseded",
                                          note=MOVED_NOTE)
                batches.add(str(entry.get("batch_key") or ""))
            elif str(entry.get("note") or "") != MISSING_NOTE:
                queue.set_needs_you_files(int(entry["id"]), images, note=MISSING_NOTE)
        for batch_key in batches:
            self._finish_batch(batch_key)
        if changed["superseded"] or changed["missing"]:
            queue.compact_schedule()  # dropped posts give their slots back
        return changed

    def _recover_ai_entries(self) -> int:
        """The once-per-start migration of "Needs you" entries (CHANGE OF PLAN).

        Re-processed automatically: entries the AI failed on (they get a fresh
        budget), and the old "not confident" ones — under the new rules those
        become a universal set instead of waiting for the artist. Entries the
        artist moved back by hand keep a note of their own and stay put.
        """
        recovered = 0
        for entry in queue.needs_you():
            if entry.get("kind") != "unknown":
                continue  # a Pixiv entry is a finished set waiting for its click
            note = str(entry.get("note") or "")
            if note and not note.startswith(AI_UNAVAILABLE):
                continue  # a set the artist pulled back, or an "original character"
            queue.resolve_needs_you(int(entry["id"]), "retrying")
            queue.forget_files(entry.get("images") or [])
            queue.ai_forget_prefix("identify:")
            queue.ai_forget_prefix("caption:")
            recovered += 1
        if recovered:
            log.info("re-processing %s older 'Needs you' entr%s under the new rules",
                     recovered, "y" if recovered == 1 else "ies")
        return recovered

    async def _split_loose(self, directory: Path, folder: dict, cards: list[str],
                           fresh: list[Path], digests: dict[Path, str], *,
                           mature: bool) -> dict:
        """Loose images: recognised ones split per character, the rest one set.

        Nothing unrecognised goes to "Needs you" any more (CHANGE OF PLAN): those
        images join one **universal** post with an engagement caption and generic
        tags, so a drop always lands somewhere.
        """
        groups: dict[str, list[Path]] = {}
        group_info: dict[str, dict] = {}
        unknown: list[Path] = []
        unknown_series: list[str] = []
        failed: list[dict] = []
        queued = 0
        waiting = 0
        for path, identity in zip(fresh, await self._identities(fresh, digests)):
            if identity is None:
                continue  # out of this pass's budget, or the AI is backing off
            if identity.get("unavailable") or identity.get("ai_error"):
                failed.append({"path": path, "identity": identity})
                continue
            if not identity.get("confident"):
                unknown.append(path)
                value = str(identity.get("series") or "").strip()
                if value and value not in unknown_series:
                    unknown_series.append(value)
                continue
            key = lazy.group_key(identity.get("series") or "", identity.get("characters") or [])
            groups.setdefault(key, []).append(path)
            group_info.setdefault(key, identity)

        for choice in failed:
            # an AI error is the one thing that still waits for a human here
            path, identity = choice["path"], choice["identity"]
            queue.add_needs_you(
                batch_key=lazy.set_key(directory, [path]), source=str(directory),
                folder_key=str(folder.get("key") or ""), kind="unknown",
                images=[str(path)], cover=str(path), caption="", tags=[],
                character=", ".join(identity.get("characters") or []),
                series=str(identity.get("series") or ""),
                platforms={}, mature=mature, note=self._note_for(identity),
            )
            queue.remember_files([path], status="needs_you")
            waiting += 1

        for key, images in sorted(groups.items(), key=lambda item: item[1][0].name):
            info = group_info.get(key) or {}
            names = [str(name).strip() for name in info.get("characters") or []
                     if str(name).strip()]
            # One recognised character is handed to the caption call as a fact, so
            # the caption cannot drift to someone else (a duo keeps the check only).
            given = ({"name": names[0], "series": str(info.get("series") or "").strip(),
                      "japanese": "", "source": "identify"} if len(names) == 1 else None)
            result = await self._queue_batch(directory, folder, cards, images, images,
                                             digests, mature=mature, group_key=key,
                                             identity=info or None, given=given)
            queued += result["queued"]
            waiting += result["needs_you"]
        if unknown:
            result = await self._universal_set(directory, folder, cards, unknown, digests,
                                               series=unknown_series, mature=mature)
            queued += result["queued"]
            waiting += result["needs_you"]
        return {"queued": queued, "needs_you": waiting}

    async def _universal_set(self, directory: Path, folder: dict, cards: list[str],
                             images: list[Path], digests: dict[Path, str], *,
                             series: list[str], mature: bool) -> dict:
        """Unrecognised loose files: one engaging post ("Universal: 9 images")."""
        return await self._mixed_batch(directory, folder, cards, images, images, digests,
                                       names=[], series=series, mature=mature, draft="",
                                       exact=False, sidecars=[], label=UNIVERSAL_LABEL,
                                       kind="universal")

    @staticmethod
    def _note_for(identity: dict) -> str:
        """Why something needs a human: the AI's fault, or a genuine "not sure"."""
        if identity.get("unavailable"):
            detail = str(identity.get("error") or "").strip()
            if detail.startswith(AI_UNAVAILABLE):
                return detail
            return f"{AI_UNAVAILABLE} — {detail or 'the model did not answer'}"
        if identity.get("disagreement"):
            return DISAGREE_NOTE
        if identity.get("original"):
            return ORIGINAL_NOTE
        if identity.get("ai_error"):
            return f"{AI_ERROR}: {identity.get('error') or 'unknown'}"
        return ""

    async def _explicit_set(self, directory: Path, folder: dict, cards: list[str],
                            batch: list[Path], fresh: list[Path], digests: dict[Path, str],
                            *, mature: bool) -> dict:
        """A subfolder is one post, and its name is the character (CHANGE OF PLAN).

        `outbox\\Changli\\` → one set named Changli, **no identify calls**; the
        caption call gets the name as written and may add the series (and the
        Japanese name for Pixiv) only when it is sure. A folder called `mix` /
        `mix-something` is a mixed post instead, identified only for its tags.
        """
        parent = batch[0].parent
        draft, exact = lazy.caption_draft(parent)
        sidecars = lazy.sidecars(parent)
        if lazy.is_mix_folder(parent.name):
            names, series = await self._mix_names(fresh, digests)
            return await self._mixed_batch(directory, folder, cards, batch, fresh, digests,
                                           names=names, series=series, mature=mature,
                                           draft=draft, exact=exact, sidecars=sidecars,
                                           kind="mixed")
        name, series = lazy.folder_identity(parent.name)  # "Citlali - Genshin Impact"
        return await self._queue_batch(directory, folder, cards, batch, fresh, digests,
                                       mature=mature, draft=draft, exact=exact,
                                       sidecars=sidecars,
                                       given={"name": name, "series": series, "japanese": ""})

    async def _mix_names(self, fresh: list[Path], digests: dict[Path, str]
                         ) -> tuple[list[str], list[str]]:
        """Who is in a `mix…` folder: identify is optional, only for the tags."""
        names: list[str] = []
        series: list[str] = []
        for identity in await self._identities(fresh, digests):
            if not identity or not identity.get("confident"):
                continue
            for name in identity.get("characters") or []:
                if str(name).strip() and str(name).strip() not in names:
                    names.append(str(name).strip())
            value = str(identity.get("series") or "").strip()
            if value and value not in series:
                series.append(value)
        return names, series

    async def _mixed_batch(self, directory: Path, folder: dict, cards: list[str],
                           batch: list[Path], fresh: list[Path], digests: dict[Path, str],
                           *, names: list[str], series: list[str], mature: bool,
                           draft: str, exact: bool, sidecars: list[Path],
                           unavailable: bool = False, given: dict | None = None,
                           label: str = "", kind: str = "mixed") -> dict:
        """Several characters in one post (or none named): a universal caption.

        Used for `mix…` folders and for the unrecognised loose files, which share
        this path so a drop always becomes a post instead of a "Needs you".
        """
        group_key = f"{kind}+{lazy.group_key('|'.join(series), names)}"
        batch_key = lazy.set_key(directory, batch, group_key)
        wants_pixiv = bool(folder.get("pixiv", True))
        sites = sorted({card.partition(":")[0] for card in cards}
                       | ({PIXIV_SITE} if wants_pixiv else set()))
        retry_key = f"caption:{batch_key}"
        error = ""
        result: dict = {}
        if unavailable:
            error = AI_UNAVAILABLE
        elif not queue.ai_ready(retry_key) or not queue.ai_available(retry_key):
            return {"queued": 0, "needs_you": 0, "retrying": True}  # still backing off
        else:
            try:
                result = await self.mixed(cfg=secrets.load(CAPTION_KEY), images=[fresh[0]],
                                          characters=names, series=series, platforms=sites,
                                          mature=mature, caption_draft=draft,
                                          caption_exact=exact, given=given)
                queue.ai_reset(retry_key)
            except captioner.CaptionUnavailable as exc:
                attempts = queue.ai_failed(retry_key)
                log.info("mixed set %s: %s (attempt %s, retrying later)",
                         batch_key, exc, attempts)
                if queue.ai_available(retry_key):
                    return {"queued": 0, "needs_you": 0, "retrying": True}
                error = f"{AI_UNAVAILABLE}: {exc}"
            except Exception as exc:  # noqa: BLE001 — a real failure: Needs you
                error = str(exc)
                log.info("mixed set %s needs a human: %s", batch_key, error)

        if not label:
            label = f"Mixed: {', '.join(names)}" if names else "Mixed"
        queue.add_batch(batch_key, str(directory), str(folder.get("key") or ""),
                        [*batch, *sidecars])
        queue.remember_files(fresh, status="queued")
        if error:
            queue.add_needs_you(
                batch_key=batch_key, source=str(directory),
                folder_key=str(folder.get("key") or ""), kind="unknown",
                images=[str(path) for path in fresh], cover=str(fresh[0]),
                caption=draft, tags=[], alt_text="", character=label,
                series=", ".join(series), platforms={}, mature=mature, note=error,
            )
            queue.remember_files(fresh, status="needs_you")
            return {"queued": 0, "needs_you": 1}

        result = dict(result)
        if draft and exact:  # word for word, whatever the model answered
            for entry in (result.get("platforms") or {}).values():
                if isinstance(entry, dict):
                    entry["caption"] = draft
        queued = self._enqueue(batch_key, directory, folder, cards, fresh, digests, result,
                               mature=mature, character=label,
                               series=", ".join(series))
        if not wants_pixiv:
            return {"queued": queued, "needs_you": 0}  # this folder skips Pixiv
        pixiv_text = (result.get("platforms") or {}).get(PIXIV_SITE) or {}
        pixiv_text = pixiv_text if isinstance(pixiv_text, dict) else {}
        queue.add_needs_you(  # Pixiv can never be hands-off
            batch_key=batch_key, source=str(directory), folder_key=str(folder.get("key") or ""),
            kind="pixiv", images=[str(path) for path in fresh], cover=str(fresh[0]),
            caption=str(pixiv_text.get("caption") or ""),
            tags=[str(tag) for tag in (pixiv_text.get("tags") or [])],
            alt_text=str(result.get("alt_text") or ""),
            character=label, series=", ".join(series),
            platforms=result.get("platforms") or {}, mature=mature,
        )
        return {"queued": queued, "needs_you": 1}

    async def _identity(self, path: Path, digests: dict[Path, str]) -> dict | None:
        """The cached or freshly asked identity of one image.

        None means "not now": either this pass has spent its identify budget, or
        the AI is backing off after a transient failure. A dict with `unavailable`
        means every retry was spent — the caller sends it to Needs you.
        """
        digest = self._digest(digests, path)
        cached = queue.identity(digest)
        if cached is not None:
            return cached
        key = f"identify:{path}"
        if not queue.ai_ready(key):
            return None
        if self.identify_calls >= IDENTIFY_LIMIT:
            return None  # the cost guard: the rest wait for the next pass
        if not queue.ai_available(key):
            return {"characters": [], "series": "", "confident": False,
                    "unavailable": True, "error": AI_UNAVAILABLE}
        escalate = self.escalations < ESCALATION_LIMIT
        if escalate:
            self.escalations += 1
        self.identify_calls += 1
        try:
            result = await self.identify(cfg=secrets.load(CAPTION_KEY), image=path,
                                         escalate=escalate)
        except captioner.CaptionUnavailable as exc:
            attempts = queue.ai_failed(key)
            log.info("identify %s: %s (attempt %s, retrying later)",
                     path.name, exc, attempts)
            return None if queue.ai_available(key) else {
                "characters": [], "series": "", "confident": False,
                "unavailable": True, "error": AI_UNAVAILABLE,
            }
        except Exception as exc:  # noqa: BLE001 — a real failure is the AI's answer
            log.info("identify failed for %s: %s", path.name, exc)
            return {"characters": [], "series": "", "confident": False,
                    "error": str(exc), "ai_error": True}
        if not result.get("confident") and result.get("escalate_skipped"):
            return None  # the escalation budget is spent: try again next pass
        queue.ai_reset(key)
        queue.remember_identity(
            digest, characters=result.get("characters") or [],
            series=str(result.get("series") or ""), confident=bool(result.get("confident")),
            model=str(result.get("used_model") or ""), source="ai",
        )
        return result

    async def _identities(self, paths: list[Path], digests: dict[Path, str]) -> list[dict | None]:
        """Identify several images with up to three requests in flight at once."""
        limit = asyncio.Semaphore(IDENTIFY_PARALLEL)

        async def one(path: Path):
            async with limit:
                return await self._identity(path, digests)

        return list(await asyncio.gather(*(one(path) for path in paths)))

    def _digest(self, cache: dict, path: Path) -> str:
        if path not in cache:
            try:
                cache[path] = lazy.digest(path)
            except OSError:
                cache[path] = ""
        return cache[path]

    def _known(self, known: dict, path: Path, cache: dict) -> bool:
        """True only while this path still holds exactly the recorded content."""
        record = known.get(str(path))
        if not record:
            return False
        try:
            stat = path.stat()
        except OSError:
            return False
        if (int(record.get("size") or 0) == int(stat.st_size)
                and float(record.get("mtime") or 0) == float(stat.st_mtime)):
            return True  # untouched since it was recorded
        return str(record.get("digest") or "") == self._digest(cache, path)

    async def _queue_batch(self, directory: Path, folder: dict, cards: list[str],
                           batch: list[Path], fresh: list[Path], digests: dict[Path, str],
                           *, mature: bool, group_key: str = "",
                           identity: dict | None = None, draft: str = "",
                           exact: bool = False, sidecars: list[Path] = (),
                           unavailable: bool = False, given: dict | None = None) -> dict:
        batch_key = lazy.set_key(directory, batch, group_key)
        retry_key = f"caption:{batch_key}"
        wants_pixiv = bool(folder.get("pixiv", True))
        sites = sorted({card.partition(":")[0] for card in cards}
                       | ({PIXIV_SITE} if wants_pixiv else set()))
        error = ""
        result: dict = {}
        names = self._set_names(identity, given)
        if sites:
            if not unavailable and queue.ai_ready(retry_key) and queue.ai_available(retry_key):
                try:
                    extra = {"caption_draft": draft, "caption_exact": exact} if draft else {}

                    async def ask() -> dict:
                        return await self.caption(
                            cfg=secrets.load(CAPTION_KEY), images=[fresh[0]],
                            platforms=sites, mature=mature, given=given, **extra,
                        )

                    result = await ask()
                    queue.ai_reset(retry_key)
                    error = await self._agree(batch_key, names, result, ask)
                    if isinstance(error, dict):  # the second try agreed
                        result, error = error, ""
                except captioner.CaptionUnavailable as exc:
                    attempts = queue.ai_failed(retry_key)
                    log.info("lazy set %s: %s (attempt %s, retrying later)",
                             batch_key, exc, attempts)
                    if queue.ai_available(retry_key):
                        return {"queued": 0, "needs_you": 0, "retrying": True}
                    error = f"{AI_UNAVAILABLE} — {exc}"
                except Exception as exc:  # noqa: BLE001 — a real failure: Needs you
                    error = str(exc)
                    log.info("lazy batch %s needs a human: %s", batch_key, error)
            elif unavailable:
                error = f"{AI_UNAVAILABLE} — the model did not answer"

        if not sites:
            error = "no sites are ticked for this folder"
        if not error and not result:
            return {"queued": 0, "needs_you": 0, "retrying": True}  # still backing off
        queue.add_batch(batch_key, str(directory), str(folder.get("key") or ""),
                        [*batch, *sidecars])
        queue.remember_files(fresh, status="queued")
        # A folder name is the artist's word: the model need not be sure of it.
        named = bool(given and str(given.get("name") or "").strip()
                     and given.get("source") != "identify")
        if error or not (result.get("confident") or named):
            platforms = result.get("platforms") or {}
            first = platforms.get(sites[0]) if sites else {}
            first = first if isinstance(first, dict) else {}
            character, series = self._who(result, identity, given)
            queue.add_needs_you(
                batch_key=batch_key, source=str(directory),
                folder_key=str(folder.get("key") or ""), kind="unknown",
                images=[str(path) for path in fresh], cover=str(fresh[0]),
                caption=str(first.get("caption") or draft),
                tags=[str(tag) for tag in (first.get("tags") or [])],
                alt_text=str(result.get("alt_text") or ""),
                character=character, series=series,
                platforms=platforms, mature=mature, note=error,
            )
            queue.remember_files(fresh, status="needs_you")
            return {"queued": 0, "needs_you": 1}

        result = dict(result)
        if draft and exact:  # the artist's words win, whatever the model answered
            for entry in (result.get("platforms") or {}).values():
                if isinstance(entry, dict):
                    entry["caption"] = draft

        # Who the set is: the folder name when the artist gave one, else the split's answer.
        character, series = self._who(result, identity, given)
        queued = self._enqueue(batch_key, directory, folder, cards, fresh, digests, result,
                               mature=mature, character=character, series=series)
        if not wants_pixiv:
            return {"queued": queued, "needs_you": 0}  # this folder skips Pixiv
        pixiv_text = (result.get("platforms") or {}).get(PIXIV_SITE) or {}
        pixiv_text = pixiv_text if isinstance(pixiv_text, dict) else {}
        queue.add_needs_you(  # Pixiv can never be hands-off
            batch_key=batch_key, source=str(directory), folder_key=str(folder.get("key") or ""),
            kind="pixiv", images=[str(path) for path in fresh], cover=str(fresh[0]),
            caption=str(pixiv_text.get("caption") or ""),
            tags=[str(tag) for tag in (pixiv_text.get("tags") or [])],
            alt_text=str(result.get("alt_text") or ""),
            character=character, series=series,
            platforms=result.get("platforms") or {}, mature=mature,
        )
        return {"queued": queued, "needs_you": 1}

    @staticmethod
    def _set_names(identity: dict | None, given: dict | None) -> list[str]:
        """Who this set is known to be before any caption is written ([] = unknown)."""
        if given and str(given.get("name") or "").strip():
            return [str(given["name"]).strip()]
        return [str(name).strip() for name in (identity or {}).get("characters") or []
                if str(name).strip()]

    async def _agree(self, batch_key: str, names: list[str], result: dict, ask):
        """"" when the caption agrees with the set's character, else one more try.

        Returns the new result when the second try agrees, or the reason (a
        string starting with DISAGREE_CAPTION) when it still does not: the set
        then goes to "Needs you" with the text so far, never out as posted.
        """
        others = queue.known_characters()
        reason = captioner.character_mismatch(result, names, others)
        if not reason:
            return ""
        log.info("lazy set %s: %s — asking once more", batch_key, reason)
        try:
            again = await ask()
        except Exception as exc:  # noqa: BLE001 — the first answer's problem stands
            log.info("lazy set %s: the second try failed too: %s", batch_key, exc)
            return f"{DISAGREE_CAPTION} — {reason}"
        second = captioner.character_mismatch(again, names, others)
        if not second:
            return again
        return f"{DISAGREE_CAPTION} — {second}"

    @staticmethod
    def _who(result: dict, identity: dict | None,
             given: dict | None = None) -> tuple[str, str]:
        """The set's character(s) and series: the artist's own name first."""
        if given and str(given.get("name") or "").strip():
            return (str(given["name"]).strip(),
                    str(given.get("series") or result.get("series") or "").strip())
        if identity:
            names = [str(name).strip() for name in identity.get("characters") or []
                     if str(name).strip()]
            series = str(identity.get("series") or "")
            if names or series:
                return ", ".join(names), series
        return str(result.get("character") or ""), str(result.get("series") or "")

    def _enqueue(self, batch_key: str, directory: Path, folder: dict, cards: list[str],
                 batch: list[Path], digests: dict[Path, str], result: dict, *,
                 mature: bool, character: str = "", series: str = "") -> int:
        data = lazy.stored()
        platforms = result.get("platforms") or {}
        items: list[dict] = []
        spacing: dict[str, float] = {}
        for card_id in cards:
            site = card_id.partition(":")[0]
            limits = lazy.limits_for(site, data)
            text = platforms.get(site) or {}
            text = text if isinstance(text, dict) else {}
            if not text:
                log.info("no caption for %s (that half of the API call failed) — skipping it",
                         card_id)
                continue
            spacing[card_id] = limits["spacing_minutes"]
            for chunk in lazy.chunks(list(batch), limits["per_post"]):
                kept = [path for path in chunk
                        if not queue.already_posted(self._digest(digests, path), card_id)]
                if not kept:
                    continue
                items.append({
                    "batch_key": batch_key,
                    "source": str(directory),
                    "folder_key": str(folder.get("key") or ""),
                    "card_id": card_id,
                    "images": [str(path) for path in kept],
                    "cover": str(kept[0]),
                    "caption": str(text.get("caption") or ""),
                    "tags": [str(tag) for tag in (text.get("tags") or [])],
                    "alt_text": str(result.get("alt_text") or ""),
                    "character": character or str(result.get("character") or ""),
                    "series": series or str(result.get("series") or ""),
                    "mature": mature,
                })
        if not items:
            return 0
        return len(queue.add_items(items, spacing_minutes=spacing,
                                   quiet=lazy.stored()["quiet"]))

    # ------------------------------------------------------------------ posting
    async def post_due(self) -> list[dict]:
        data = lazy.stored()
        if not data["enabled"] or data["paused"]:
            return []
        now = time.time()
        results: list[dict] = []
        self.reconcile()  # a file moved since the last scan must not post empty
        for entry in queue.due_items(now):
            card_id = str(entry["card_id"])
            site = card_id.partition(":")[0]
            limits = lazy.limits_for(site, data)
            if not queue.card_ready(card_id, limits["spacing_minutes"], now):
                continue  # the PC was off: catch up one post at a time
            results.append(await self._post(entry, card_id))
        return results

    async def _post(self, entry: dict, card_id: str) -> dict:
        # The last guard: only images that exist and this card has not had yet.
        wanted = [str(path) for path in entry.get("images") or []]
        kept = [path for path in wanted if Path(path).exists()
                and not queue.already_posted(self._digest({}, Path(path)), card_id)]
        if not kept:
            missing = any(not Path(path).exists() for path in wanted)
            status, note = ("missing", MISSING_NOTE) if missing else ("superseded",
                                                                     "already posted here")
            queue.set_item_files(int(entry["id"]), wanted, status=status, error=note)
            self._finish_batch(str(entry.get("batch_key") or ""))
            return {"item": entry["id"], "card_id": card_id, "ok": False, "error": note}
        if kept != wanted:
            queue.set_item_files(int(entry["id"]), kept)
            entry = {**entry, "images": kept, "cover": kept[0]}
        post_id = self._stage(entry)
        images = [path for path in _images(post_id)]
        # "Mixed: A, B…" / "Universal" is a Queue-tab label, not a name: sites
        # would turn it into a title (DeviantArt) or a tag (Pixiv).
        mixed = is_mixed(entry)
        post = Post(
            images=images,
            caption=str(entry.get("caption") or ""),
            tags=parse_tags(entry.get("tags") or []),
            alt_text=str(entry.get("alt_text") or ""),
            character="" if mixed else str(entry.get("character") or ""),
            series="" if mixed else str(entry.get("series") or ""),
            options={"mature": bool(entry.get("mature")), card_id: {"style": "ai"}},
        )
        try:
            result = await self.publish(card_id, post, post_id, False)
        except Exception as exc:  # noqa: BLE001 — the item keeps the error for a retry
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ok = bool(result.get("ok"))
        if ok:
            for path in entry.get("images") or []:
                queue.remember_posted(self._digest({}, Path(path)), card_id,
                                      str(result.get("url") or ""))
            queue.mark_posted(int(entry["id"]), str(result.get("url") or ""),
                              str(result.get("note") or ""))
            queue.mark_files(entry.get("images") or [], "posted")
        else:
            error = str(result.get("error") or "failed")
            queue.mark_failed(int(entry["id"]), error)
            # A failure leaves the slot open on purpose: the next item may go as
            # soon as it is due, so one broken site cannot hold the queue back.
            log.warning("lazy item %s for %s failed (the card's next post is not delayed): %s",
                        entry["id"], card_id, error)
        self._finish_batch(str(entry.get("batch_key") or ""))
        return {"item": entry["id"], "card_id": card_id, "ok": ok,
                "url": result.get("url"), "error": result.get("error")}

    def _stage(self, entry: dict) -> str:
        """Copy an item's images into their own upload folder (a stable post id)."""
        post_id = uuid.uuid4().hex
        folder = paths.upload_dir(post_id)
        folder.mkdir(parents=True, exist_ok=True)
        for index, source in enumerate(entry.get("images") or [], start=1):
            path = Path(source)
            if not path.exists():
                continue
            name = path.name[:70]
            (folder / f"{index:02d}_{name}").write_bytes(path.read_bytes())
        return post_id

    def _finish_batch(self, batch_key: str) -> None:
        """When every item of a batch is done, move its files to posted/.

        An open 'Needs you' entry keeps the files where they are: the Pixiv flow
        opens from that entry and needs the originals until it is resolved.
        """
        if not batch_key or queue.batch_pending(batch_key):
            return
        if queue.open_needs_you(batch_key):
            return
        entry = queue.batch(batch_key)
        if entry is None or entry.get("moved_at"):
            return
        files = [Path(path) for path in entry.get("files") or []]
        if not any(path.exists() for path in files):
            # every file moved on (to a newer set) or was deleted: nothing to file away
            queue.mark_batch_moved(batch_key)
            return
        links = queue.batch_links(batch_key)
        folder = lazy.move_to_posted(files, name=self._posted_name(batch_key, entry, files))
        (folder / "links.json").write_text(
            json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8")
        queue.mark_batch_moved(batch_key)
        # The paths are free again: a *new* file called 1.png must be seen as new.
        queue.forget_files(files)
        # The emptied subfolder is gone: outbox/ only shows work still waiting.
        lazy.remove_if_empty(
            {path.parent for path in files},
            roots=[lazy.resolve(item.get("path") or "") for item in lazy.stored()["folders"]],
        )

    @staticmethod
    def _posted_name(batch_key: str, entry: dict, files: list[Path]) -> str:
        """Where the finished set goes: its own folder name, or its character's."""
        source = str(entry.get("source") or "")
        parents = {path.parent for path in files}
        if len(parents) == 1:
            parent = next(iter(parents))
            if str(parent) != source:
                return parent.name  # a subfolder set keeps its folder name
        names = queue.batch_characters(batch_key)
        return names[0] if names else "set"


MIXED_KINDS = ("mixed", "universal")


def is_mixed(entry: dict) -> bool:
    """True for a queue item or 'Needs you' entry of a mixed or universal set."""
    key = str(entry.get("batch_key") or "")
    label = str(entry.get("character") or "")
    return (any(f"+{kind}+" in key for kind in MIXED_KINDS)
            or label == UNIVERSAL_LABEL or label == "Mixed" or label.startswith("Mixed: "))


def mixed_names(entry: dict) -> dict | None:
    """The characters and series behind a mixed entry's label, or None if not mixed."""
    if not is_mixed(entry):
        return None
    label = str(entry.get("character") or "")
    names = label[len("Mixed: "):] if label.startswith("Mixed: ") else ""
    return {
        "characters": [name.strip() for name in names.split(",") if name.strip()],
        "series": [name.strip() for name in str(entry.get("series") or "").split(",")
                   if name.strip()],
    }


def _images(post_id: str) -> list[Path]:
    folder = paths.upload_dir(post_id)
    return sorted(path for path in folder.glob("*")
                  if path.is_file() and path.name[:2].isdigit())
