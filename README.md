# ArtDrop

Post your artwork once, send it everywhere. ArtDrop is a small local app that
runs on your own PC: drop the images, let the optional Caption AI write the text,
and publish to your art accounts from one page — or switch on **lazy mode** and
let it watch a folder (`Pictures\ArtDrop\outbox`) and post from there on a
spread-out schedule. Nothing runs in the cloud, there is no server to pay for,
and your passwords never leave Windows Credential Manager.

**Supported sites:** Bluesky, Telegram, Discord, Tumblr, DeviantArt, Instagram
(multiple accounts, with collab invites) and Pixiv (through a real browser
window, semi-automatic by default). X/Twitter is not included (its posting API is
paid per use) and Reddit is not built yet.

## Requirements

- **Windows 10 or 11** (the app, the launcher and the `Pictures\ArtDrop` outbox
  path assume Windows).
- **Python 3.11 or newer** — the launcher falls back to 3.12 or 3.11 if your
  default Python is newer than a dependency supports. `py` (the Python launcher)
  must be on PATH.
- **Google Chrome** for Pixiv: ArtDrop opens Pixiv's sign-in page in your own
  Chrome (Cloudflare refuses automated browsers) and only uses its bundled
  Playwright/Chromium for the upload form. Install Chrome first, or Edge as a
  fallback.
- An OpenCode Go API key if you want the Caption AI (everything else works
  without it).

## Quick start

1. Download or clone this repository into a folder of your choice.
2. Double-click **`Start ArtDrop.bat`**. The first run creates a virtual
   environment, installs the dependencies, downloads the fallback browser for
   Pixiv and then opens http://127.0.0.1:8765/ in your browser.
3. On the **Accounts** tab, connect the sites you want (each site has its own
   short guide) and optionally add your Caption AI key.
4. Drop images on the **Post** tab, pick the sites and press **Post**.

Guides:

- [`docs/ArtDrop-Setup-Guide.pdf`](docs/ArtDrop-Setup-Guide.pdf) — every account
  and API key, click by click, with the exact settings each site wants.
- [`docs/ArtDrop-User-Guide.pdf`](docs/ArtDrop-User-Guide.pdf) — how to use the
  app day to day: posting, caption styles, lazy mode, history.
- [`SETUP.md`](SETUP.md) — the same setup steps as plain text (and the
  troubleshooting table).

## Lazy mode (optional)

Switch it on in the **Queue** tab and drop images into `Pictures\ArtDrop\outbox`
(or `outbox-instagram` for pieces that are safe for Instagram). Files that land
together are grouped, captioned by the AI in one call, split per character when a
drop mixes them, queued per site and posted at a sensible spacing (two hours per
site by default, never faster than 30 minutes). Pixiv always waits for you:
those items appear under *Needs you* so you can pick the rating and press Post
yourself.

## Disclaimer

- **Pixiv has no public posting API.** ArtDrop fills Pixiv's real upload form in
  a real browser window and, in the default semi-automatic mode, **you** press
  the Post button. That is the only reason it works at all — use this feature at
  your own risk and follow Pixiv's own rules.
- **You are responsible for each site's rules** about AI-generated and mature
  content. ArtDrop sets the AI-generated label where a site offers one, marks
  mature posts as mature where the API allows it, and warns you when a choice
  looks inconsistent, but the decision — and the responsibility — is yours.
- ArtDrop is **not affiliated with, endorsed by or sponsored by** any of the
  sites it posts to. Site names and logos belong to their owners.
- The software is provided under the MIT licence (see [`LICENSE`](LICENSE)), with
  no warranty: check what it is about to do (the **Dry run** button posts
  nothing) and keep your own copies of your art.
