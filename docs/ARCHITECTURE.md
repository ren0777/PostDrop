# ArtDrop — architecture

A short map of the code for anyone reading it (or contributing). ArtDrop is a
local, single-user app: one Python process serves a small web UI on
`127.0.0.1:8765`, and everything (secrets, history, queue, browser profiles)
stays on the machine.

## Layout

```
app/
  main.py            FastAPI app: routes, the one publish path, the queue API
  watch.py           Lazy mode's background worker (folders -> queue -> posts)
  core/
    paths.py         data folders and the local address
    secrets.py       Windows Credential Manager (keyring); never files
    settings.py      non-secret settings (data/settings.json)
    accounts.py      per-site accounts ("<site>:<key>" keyring entries)
    post.py          the Post model, tag parsing, caption clipping
    images.py        prepare(): passthrough, or re-encode within a site's limits
    captioner.py     Caption AI (OpenCode Go): captions, tags, alt text, identify
    oauth.py         the shared OAuth helper (loopback + paste-the-code fallback)
    browser.py       Playwright on one dedicated thread, one context per profile
    history.py       data/history.sqlite (one row per attempt)
    queue.py         data/queue.sqlite (lazy items, slots, hashes, identities)
    lazy.py          lazy-mode settings, folder scanning, batches and sets
    applog.py        data/artdrop.log (rotating, secrets scrubbed)
    imagehost.py     swappable public image host (imgbb first, for Instagram)
  platforms/
    base.py          Connector: fields, preparing images, publish(), scrub()
    bluesky.py telegram.py discord.py tumblr.py deviantart.py instagram.py pixiv.py
    oauth.py         OAuthConnector on top of core/oauth.py
web/                 index.html, app.js, style.css (no build step, no framework)
tests/               pytest, no network and no real browser needed
tools/               helper scripts the launcher uses
docs/                this file plus the two PDF guides
```

## The pieces that matter

**Connectors.** Every site is a `Connector` subclass: it declares its fields,
limits (`max_images`, `max_bytes`, `max_side`, `original_formats`, `png_formats`,
`max_tags`, `caption_limit`) and a `send(post, cfg, text, images) -> (url, note)`.
`base.publish()` wraps it with the dry run, the image preparation and the error
text. A connector never sees the keyring directly; `cfg` is handed to it.

**One publish path.** `app/main.py:publish_card()` is the only way a post goes
out: it takes a per-site lock, checks `data/uploads/<post_id>/status.json` (the
double-post guard), publishes, records the status and writes History. The Post
tab's route and the lazy queue both call it.

**The web UI** is plain HTML/JS/CSS. `app.js` renders the site cards from the
`/api/platforms` payload, so a connector's fields appear without touching the
frontend. Nothing is cached server-side: the app is one user on localhost.

**Caption AI** (`core/captioner.py`) talks to OpenCode Go's Chat Completions API.
Recognition uses small images (512 px, 300 KB) and caption calls attach at most
four; timeouts, 429 and 5xx come back as `CaptionUnavailable`, which the lazy
worker retries with a 1/5/15-minute backoff before it asks the user ("Needs you",
with a **Retry AI** button).
`generate()` writes captions/tags/alt text per selected site, `generate_lazy()`
also recognises the character from the image, `generate_mixed()` writes the
universal caption for a post that mixes several characters, `identify()` answers
"who is in this picture?" for the folder splitter. The artist's own text can be
passed as a draft (polished per site) or exact (used word for word).

**Lazy mode** is three parts: `core/lazy.py` (settings, folders, batches),
`core/queue.py` (SQLite: items, per-card slots, quiet hours, file/hash memory,
the identify cache) and `watch.py` (the worker that scans, captions, queues and
posts what is due). A subfolder is one post and its name is the character (a
trailing counter is stripped; `mix…` folders get the universal caption); loose
files are split per recognised character, and whatever cannot be recognised
becomes one "Universal" post instead of a "Needs you" entry. "Needs you" is only
for Pixiv, AI errors that outlived their retries, and sets the artist moved back.
Finished sets move to `posted/<date>/<name>/` (the subfolder or character name) with
a `links.json`; the emptied subfolder is removed, and a per-folder switch can skip Pixiv.

**Browsers (Pixiv).** `core/browser.py` runs Playwright on **one dedicated
thread**, with one Playwright per process and one open context per profile, so
objects never cross threads and a window is never launched twice. Signing in uses
the user's own Chrome (`subprocess.Popen`) because Cloudflare refuses automated
browsers; Playwright only checks the login and fills the upload form.

**Security posture.** Secrets live in the OS keyring; error text is scrubbed
(`base.scrub`) before it reaches the UI or the log; the API requires the
`X-ArtDrop: 1` header on writes and a localhost `Host`; the log file redacts
labelled secrets even inside tracebacks.
