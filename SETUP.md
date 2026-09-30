# ArtDrop — setup guide

ArtDrop posts your art to several sites from one page. You drop in an image, write
a caption (or let the Caption AI write it), tick the sites, and press **Post**.
It covers **Bluesky**, **Telegram**, **Discord**, **Tumblr**, **DeviantArt**,
**Instagram** and **Pixiv** — and with **lazy mode** on it can watch a folder and
post from there by itself (section 5).

Nothing leaves your PC except the posts themselves. There is no account, no cloud,
no subscription.

---

## 1. Start the app

1. Open the folder where you extracted ArtDrop (the one that contains
   `Start ArtDrop.bat`).
2. Double-click **`Start ArtDrop.bat`**.
3. The first run takes a minute: it creates a private Python environment and
   installs what it needs. You only see this once.
4. Your browser opens at **http://127.0.0.1:8765/** — that's ArtDrop.
5. **Keep the black console window open** while you use the app. When you close
   it, the app stops.

If Windows asks whether to allow Python through the firewall: it doesn't need
network access from outside — the app only listens on your own PC. "Cancel" is fine.

> If double-clicking the .bat flashes a window and closes, run it again and read
> the message; the window stays open when something goes wrong.

## 2. Connect your accounts

Open the **Accounts** tab. Each site has its own card with Save / Test connection /
Remove, and a short "How to set this up" section. Full steps below.

### Bluesky (about 2 minutes)

1. Open bluesky.com (or the app) and log in.
2. **Settings → Privacy and security → App passwords → Add App Password**.
3. Name it "ArtDrop" and copy the password it shows (looks like `xxxx-xxxx-xxxx-xxxx`).
4. In ArtDrop's **Accounts** tab, Bluesky card:
   - **Handle**: your handle, e.g. `yourname.bsky.social`
   - **App password**: paste the password from step 3
5. **Save**, then **Test connection**. You should see: `Connected as @yourname.bsky.social`.

Never paste your real Bluesky password — the app password can be revoked any time
from the same settings page.

### Telegram (about 3 minutes)

1. In Telegram, message **@BotFather** and send `/newbot`.
2. Give it a name and a username (must end in `bot`). BotFather replies with a
   **token** like `123456789:AA...`. Copy it.
3. Create a channel (or use one you already own). Add the bot to it and make it an
   **administrator** — a normal member can't post.
4. Find the chat id:
   - **Public channel** (`@yourchannel`): just use the `@username`.
   - **Private channel**: post any message in the channel, then open
     `https://api.telegram.org/bot<TOKEN>/getUpdates` in your browser (replace
     `<TOKEN>`). Look for `"chat": {"id": -100...}` and copy that number,
     including the minus sign.
5. In ArtDrop's **Accounts** tab, Telegram card: paste the **Bot token** and
   **Chat id**.
6. **Save**, then **Test connection**. You should see something like
   `Bot @yourbot → chat “Your Channel”`.

Tip: use a private test channel first, then point the same bot at your real one.

### Discord (about 1 minute)

1. In Discord, right-click the channel you want → **Edit Channel → Integrations →
   Webhooks → New Webhook**.
2. Name it (this name shows above posts) and **Copy Webhook URL**.
3. In ArtDrop's **Accounts** tab, Discord card: paste the **Webhook URL**.
   Optionally set a **Display name** (default: ArtDrop).
4. **Save**, then **Test connection**. You should see
   `Webhook “your-name” (channel 123...)`.

Treat the webhook URL like a password: anyone who has it can post to that channel.
If it leaks, delete the webhook in Discord and make a new one.

### Tumblr (about 5 minutes)

1. Log in to Tumblr, then open **tumblr.com/oauth/apps** and press **Register
   application**.
2. Fill in any name and website (e.g. `https://example.com`). In the
   **OAuth2 redirect URLs** box add exactly:
   `http://localhost:8765/oauth/tumblr`
   (Tumblr rejects IP-address URLs like `127.0.0.1`, so use `localhost`.)
3. Save. Copy the **OAuth2.0 Client ID** and **Client Secret**.
4. In ArtDrop's **Accounts** tab, Tumblr card: paste your **blog**
   (e.g. `yourname.tumblr.com`), the client ID and secret, then **Save**.
5. Press **Connect**. Tumblr asks you to allow access — press Allow. The tab
   will say *connected ✓*.
6. Press **Test connection**: `Connected as … — posting to yourname.tumblr.com`.

If Tumblr will not accept that URL either: register any `https://` URL instead,
put it into the **Redirect URI** box on ArtDrop's Tumblr card, press **Connect**,
then copy the whole address from the browser's address bar into the "paste the
code" box and press **Finish connection**.

Tip: tick **Post as a draft** on the site's card in the Post tab for your first
test — a draft proves everything works without showing up on your blog.

**Mature posts:** Tumblr's API cannot set community labels (their own docs and
open issues confirm it), so when you post something mature ArtDrop reminds you
on the card: open the post in the Tumblr app and switch the label to **Mature**.
For a draft, set the label when you publish it.

### DeviantArt (about 5 minutes)

1. Open **deviantart.com/developers/applications** → **Register application**.
2. Give it a name and set the client type to **Confidential**.
3. Add the redirect URI **`https://127.0.0.1/artdrop`** — DeviantArt only accepts
   `https` URLs, so this is the one to use everywhere. Tick the scopes
   **Basic information**, **Stash** and **Publish** (the values behind them are
   `basic`, `stash` and `publish`).
4. Copy the **client ID** and **client secret** into ArtDrop's DeviantArt card
   (leave the Redirect URI field empty to use the default), then **Save**.
5. Press **Connect**, allow access, then copy the whole address you land on from
   the browser's address bar into the box on the card and press
   **Finish connection**. Check the tab says *connected ✓*, then
   **Test connection**: `Connected as yourname`.
6. On the site's card in the Post tab you can set a **Title**, a **Gallery folder
   ID** (the folder's UUID, from its DeviantArt URL — optional), **Mature (18+)**
   and a **Mature level** (moderate or strict). For the first test, tick
   **Save to Sta.sh only** — nothing is published, and History shows a sta.sh
   link you can look at. The Title defaults to `<Character> — <Series>` (or the
   first caption line), and **✨ Generate** writes a short AI title into the box.
7. If DeviantArt asks for the submission policy / terms agreement, ArtDrop
   accepts both automatically and retries once; the card will say so.

Note: a deviation is a single file, so DeviantArt accepts **one image** per
post. ArtDrop will tell you if you select more.

### Instagram (about 10 minutes)

Instagram's API needs an Instagram **Professional** account (Business or
Creator) — switching is free in the Instagram app under Settings → Account type.
No Facebook Page is needed.

1. Open **developers.facebook.com/apps** → **Create app** and pick the
   **Instagram** use case ("Instagram API with Instagram login").
2. In the app's Instagram settings (API setup with Instagram login):
   - press **Generate access tokens → Add account** to attach your Instagram
     account — the old "Instagram Tester" role is retired and its form won't
     save on new apps;
   - add **https://localhost/artdrop** as the redirect URI — exactly, **with no
     trailing slash**, or Instagram rejects the authorize step;
   - note the **Instagram app ID** and **app secret**.
3. Get a free **imgbb** key: sign in at imgbb.com, open **api.imgbb.com** and
   press *Get API key*. Instagram fetches images from a public URL, so ArtDrop
   parks each picture on imgbb for one hour while the post goes out.
4. In ArtDrop's **Accounts** tab, Instagram card: paste the app ID, app secret,
   the redirect URI you registered, and the imgbb key. **Save**. (If the URI ends
   with a `/`, ArtDrop warns you — Instagram only accepts the exact registered
   URI.)
5. Press **Connect**, allow access, and check the tab says *connected ✓*, then
   **Test connection**: `Connected as @yourname (id …)`.
6. Post as usual: one image, or up to 10 for a carousel. ArtDrop pads tall or
   wide images with white to Instagram's 4:5 … 1.91:1 window and cuts the
   caption to 2200 characters with at most 30 hashtags.

The same paste-the-code fallback works if Meta refuses the redirect: register
any `https://` URL instead, paste it into the Redirect URI box, press Connect and
copy the address bar contents into the code box. Long-lived tokens last 60 days;
ArtDrop refreshes them by itself once a week is left.

**More than one account.**

- Add each Instagram account to the Meta app first (API setup with Instagram
  login → **Generate access tokens → Add account**).
- On the Accounts tab, press **Add Instagram account**, give it a name (e.g.
  *Solo*) and press Connect while logged in to that account in the browser.
- Every account gets its own card in the Post tab — "Instagram — Solo",
  "Instagram — Art" — with its own tick, caption style, overrides, status and
  History entries. The double-post guard works per account.
- Upgrading from the very first ArtDrop version moves that single account to one
  named *Main*; its invite settings start empty, so tick what you want (below).

**Collab invites.** On the account that should invite others, tick **Can send
collab invites** and list up to 3 default usernames (without `@`). The post card
shows a live **Will invite: @…** line under the box (the dry run repeats it), and
**clearing the box sends that post with no collaborator** — the default comes
back on a fresh page. The invited account gets an invitation in the Instagram app
and has to accept it for the post to appear on its profile too. Both accounts
must be Professional (Business or Creator).

### Pixiv (experimental — drives a real browser window)

Pixiv has no public upload API, so ArtDrop drives the real upload form in a
browser window. Nothing goes through private APIs or saved cookies — the session
lives in a private browser profile on your PC, and ArtDrop never sees your
password.

1. On the Accounts tab, press **Add Pixiv account**, name it, then press **Log in**.
   Pixiv's sign-in page opens in **your own Chrome** — Cloudflare refuses a test
   browser, so ArtDrop never automates the sign-in itself.
2. Sign in normally in that Chrome window — CAPTCHA and 2FA included.
   **If you normally log in to Pixiv with Google**, set a Pixiv password first
   (pixiv.net → Settings → Account → Password), then sign in here with email +
   password — Google blocks sign-in from a browser it doesn't trust.
3. **Close that Chrome window**, then press **Check login** on the card. ArtDrop
   opens the upload page in the background and the badge turns *logged in*.
   (If the window is still open, **Log in** just says so — nothing is started
   twice.)
4. Repeat for the second account: **Add Pixiv account** → Log in → close Chrome →
   Check login.
5. **Forget login** deletes the profile; **Remove** deletes the account row. Both
   ask you to close the Pixiv Chrome window first while it still has the profile
   open.

Posting:

- The card has **All ages (全年齢) / R-18 / R-18G** buttons. Pixiv requires a
  rating on every post, so ArtDrop refuses to post until you pick one — the
  rating you used last time is only shown as a dashed hint. Choosing R-18 can
  reveal extra required options on Pixiv's page: ArtDrop stops there and leaves
  the window open so you can fill those in.
- Unless **Full auto** is ticked on the account, the form is filled and left for
  you: press **Post** yourself in the window. The card says *waiting for the
  Pixiv window…* until you do. Pixiv reloads the create form instead of opening
  the new work, so ArtDrop takes the link from Pixiv's own answer and, if that is
  missed, from your works page — either way History gets the artwork link.
  Closing the window first is reported as *not submitted*.
- ArtDrop always sets Pixiv's **AI-generated work** switch, uses the Japanese
  title (✨ Generate writes it), your caption and up to **10 tags** — at least
  one, because Pixiv requires one (the character or series name is used when
  there is nothing else; spaces in a name are removed, because Pixiv's tag box
  splits tags on spaces) — and never sends AI-disclosure tags like `aiart`,
  because Pixiv labels AI works with its own switch. Originals are sent untouched
  unless they are over 32 MB, **WEBP becomes PNG** (Pixiv takes only JPEG, PNG
  and GIF), and one account never posts more than once every **three minutes**.
- If a field can't be found or can't be verified (Pixiv changes its page), the
  window stays open and the card tells you which field to fill by hand — ArtDrop
  never guesses. That includes the tag counter: the tags must show up in Pixiv's
  own `0/10` counter.
- Ticking **Mature** and choosing **All ages** gives a warning: Pixiv requires
  R-18 for sexual content.

### Caption AI (optional — needs an OpenCode Go key)

Caption AI looks at the artwork and writes a caption, tags and alt text for each
site you tick. ArtDrop works fine without it; this card is just a time saver.

1. Go to **opencode.ai**, sign in, open the **Console → Go** and copy your API
   key. (Same subscription you already use in OpenCode; these requests count
   toward its monthly limit.)
2. On ArtDrop's **Accounts** tab, open the **✨ Caption AI** card, paste the key
   and press **Save**.
3. Press **Test connection**. It sends one tiny solid-red image to both models
   and checks they answer "red", which proves they really see images:
   `Caption AI ready — vision deepseek-v4.1-flash ✓, backup mimo-v2.6-flash ✓`

The vision model reads the picture first; the backup model takes over if it
fails or refuses the image. If both refuse (a very suggestive picture, say), the
backup writes from the character name alone and the app says *"written from name
only"*. Both model names are editable — if one stops working, look up the current
list at opencode.ai/docs/go and paste another id (`glm-5.3-flash`,
`mimo-v2.6-flash` and `kimi-k3` were also checked and do see images). A retired
model is reported plainly, e.g. *model not available (retired or renamed) — pick
another model in Accounts → Caption AI*.

## 3. Your first post

1. On the **Post** tab, drop an image into the box (or click to browse).
   You can also drop a **`.txt` file next to it** — ArtDrop fills the caption and
   tags from it. The format is just:
   ```
   A quiet afternoon in the clouds.

   tags: art, illustration, clouds
   ```
2. Optional and if Caption AI is set up: type the **character** (and the series)
   and press **✨ Generate**. Every ticked site gets its own caption and tags in
   the box under *"Caption and tags for …"*, and the alt text is filled too.
   Everything stays editable, and Generate only overwrites boxes you changed
   after a confirm.
3. Write or edit the **caption** and **tags** yourself if you prefer. Each site's
   card shows a live counter (`215 / 300`) against that site's own limit;
   ArtDrop trims tags from the end when a caption is too long.
4. Optional **Alt text** — a short description of the image for accessibility.
5. Tick the sites you want. Your ticks and the dry-run switch are remembered.
6. **First time: use Dry run.** Tick "Dry run", press Post. Every site will show
   what *would* be posted — the exact text and the prepared image sizes — and
   nothing is sent anywhere.
7. Untick Dry run and press **Post** for real.

During a post, each site's card shows its own status. If one site fails, the
others still go out; failed cards get a **Retry** button, and once a site shows
**posted** it locks. Pressing Post again only sends to sites that don't have the
post yet — pressing **New post** clears everything for your next piece.

### Caption styles (one per site)

Every site card has a **Caption style** dropdown with three choices:

| Style | What gets posted | Default for |
|---|---|---|
| **AI caption** | The caption (shared or per-site) plus its tags | Bluesky, Discord, Tumblr, DeviantArt |
| **Name + series** | Only `Stelle — Honkai: Star Rail` — no description, no tags | Telegram |
| **Images only** | No text at all | — |

For **Name + series** and **Images only**, the caption and tags boxes are hidden
and the card shows a live preview of exactly what will be posted. The name comes
from the **Character** box on the Post tab, the series from the **Series** box —
and if you leave the series empty, **✨ Generate** fills it in with the series the
AI recognizes (as long as at least one site uses AI captions).

Styles are saved on your PC in `data\settings.json` and are remembered between
restarts (the Post tab's ticks stay in the browser). The same styles will drive
the folder "lazy mode" later.

The **Caption AI** card has an **Always add an AI tag (aiart)** switch if you ever
want to stop adding that tag.

The **History** tab lists every attempt, with a link to each real post.

## 4. What each site needs (quick reference)

| | Bluesky | Telegram | Discord | Tumblr | DeviantArt | Instagram | Pixiv |
|---|---|---|---|---|---|---|---|
| Max images | 4 | 10 | 10 | 10 | 1 | 10 | 30 |
| Caption limit | 300 chars | 1024 chars | 2000 chars | 4096 chars | 65 535 chars | 2200 chars | 4096 chars |
| Image size limit | ~1 MB each | 9.5 MB each | 9.5 MB each | 20 MB each | 30 MB each | 8 MB each | 32 MB each |
| Image files | JPEG, PNG | JPEG, PNG | JPEG, PNG, WebP, GIF | JPEG, PNG, WebP | JPEG, PNG, WebP, GIF | JPEG (padded to 4:5 … 1.91:1) | JPEG, PNG, GIF (WebP becomes PNG) |
| Posting as | you | a bot | a webhook | your blog | your account | your account | a browser window |
| Default caption style | AI caption | Name + series | AI caption | AI caption | AI caption | AI caption | AI caption |

**More images than a site allows?** ArtDrop posts the first ones and says so on
the card and in History — Bluesky takes the first 4, DeviantArt the first 1,
Instagram up to 10 (as a carousel).

ArtDrop resizes/re-encodes automatically when a file doesn't fit: transparency is
flattened onto white, and it becomes a JPEG.

## 5. Lazy mode (drop images in a folder)

Off until you switch it on (**Queue** tab → *Post from my outbox folders
automatically*). While it is on, a banner sits above every tab.

**The easy way — one folder per post (this is what the artist uses):** make a
subfolder inside the outbox and name it after the character:
`Pictures\ArtDrop\outbox\Changli\`. Everything inside is **one post**, the folder
name **is** the character's name, and no recognition call is needed at all.
For repeat posts, add a counter in one of these forms — `Changli (2)`,
`Changli [2]`, `Changli_2`, `Changli-2`, `Changli #2` or `Changli v2` — and it is
stripped, so every one of them means "Changli". A plain `Changli 2` is left
alone, because the number may be part of the name (`Android 18`, `Area 51`).

For a post with **several characters together**, call the folder **`mix`** or
**`mix-something`** (e.g. `outbox\mix-morning\`): it gets a universal,
engagement-style caption that fits all of them ("Which one is your fav? Comment
your fav below! 💬") and tags every character the AI could name.

Folders whose name starts with **`_`** (e.g. `_EXAMPLE Character Name`) are your
own notes: ArtDrop never scans them and never lists them as stuck.

`caption.txt` (plain text) in any subfolder writes the caption yourself — it
moves to `posted\` with the set, and there are two modes:
- **Polish (default):** your text is a **draft**. The AI keeps your meaning,
  tone and call-to-action, fixes the grammar, adds a fitting emoji or two and
  adapts it per site (short on Bluesky, a Japanese rendering on Pixiv). Tags
  are added as usual.
- **Exact:** start the file with the line **`!exact`** to use the rest
  **word for word** on every site (the line itself is dropped). Only the tags
  are written, and long text is still cut to each site's limit.

The same choice exists on the Post tab: **Use my caption as-is** next to the
Caption box (default off = ✨ Generate polishes what you typed).

**Loose files** (dropped straight into the outbox, no subfolder) still work:
files that land together within **3 minutes** count as one drop, and ArtDrop
asks about each image and posts one set per recognised character. Anything it
cannot recognise does **not** wait for you: all unrecognised images of a drop
become **one "Universal" post** with an engagement caption and general tags
(animeart, animegirl, fanart…), plus any series it was sure about.
**What happens next:**
1. ArtDrop notices the files within a few seconds and makes its Caption AI calls
   (identify first, then one caption per set).
2. **Pixiv always waits for you**: every batch also appears under *Needs you:
   Pixiv* — one click opens the normal Post tab with the Japanese text filled in;
   you pick the rating and press Post. Switch **Also prepare for Pixiv** off for a
   folder you never take to Pixiv: then no entry is made, and the files move on as
   soon as the other sites are done (the Queue tab says "Waiting for your Pixiv
   decision" while such an entry is the last thing left).
3. If the AI cannot answer at all (an error that outlived its retries), the batch
   waits under *Needs you* with the reason and a **Retry AI** button.
4. When all of a batch's posts are done (or skipped), the files move to
   `Pictures\ArtDrop\posted\<date>\<name>\` — a subfolder set keeps its folder
   name, loose sets use the character's (or `Universal`) — with a `links.json` of
   the post links inside. The emptied subfolder is then removed from the outbox
   (only if it is completely empty, never an outbox root, never a `_` folder) —
   unless the **Pixiv** entry is still waiting; that one needs the originals, so
   they stay until you open it or dismiss it. Files are remembered by **content**,
   not by name: a later `1.png` (even the same name again) is picked up as new.

Queue tab controls: **Post now**, **Edit caption**, **Skip**, **Retry**,
**Remove**, and a global **Pause lazy mode**. The settings there let you edit the
folder paths, which cards each folder feeds (per account), mature-by-default per
folder, images per post and spacing (**never below 30 minutes**), and optional
**quiet hours** (default off; no posts between 01:00 and 08:00 — they wait for the
window to end). A **Stuck** section lists anything in a folder ArtDrop will never
post (a PDF, a PSD, a stray `.json`) so nothing sits there silently.

**Safety:** nothing posts twice — each file's content hash is remembered per card,
and a site that already succeeded for a piece is final. No site ever posts more
often than its spacing, so a PC that was off overnight catches up one post at a
time instead of a burst. Lazy mode posts only while ArtDrop is open.

## 6. Troubleshooting

| Problem | What to do |
|---|---|
| The browser shows "can't reach this page" | Check the console window is still open, then refresh. |
| "Address already in use" | ArtDrop is already running — just open http://127.0.0.1:8765/. |
| "Test connection: Telegram getChat failed" | The bot isn't an admin of the channel, or the chat id is wrong. |
| "Bluesky login failed" | Use an **app password**, not your account password. |
| "Not set up yet — missing: …" | Go to Accounts, fill in the named fields, Save. |
| Post failed but Discord shows it twice | It didn't — check History; a failed status can be a network blip after a real post. |
| DeviantArt says "Incorrect authorization code" | Press **Connect** again and paste the **newest** address from the browser — DeviantArt's PKCE challenge is only valid for the last Connect (ArtDrop handles PKCE for you). |
| The page looks out of date after an ArtDrop update | Hard-refresh it (**Ctrl+F5**) so the browser reloads `app.js` instead of its cached copy. |
| Pixiv's sign-in page keeps showing "Verify you are human" | That is a Cloudflare challenge aimed at automated browsers. ArtDrop never signs in with Playwright: press **Log in** and sign in in the Chrome window it opens. |
| "a Chrome window still has this Pixiv account open — close it first" | Close the Pixiv Chrome window (the one on this account's profile), then press the button again. If no window is open, delete the `lockfile` in `data\pixiv-profiles\<account>`. |
| Pixiv says a component isn't installed | A new ArtDrop version needs components your `.venv` predates. Close the console and start ArtDrop again with `Start ArtDrop.bat`; it prints *Updating ArtDrop's components…* and installs them. |
| A Pixiv post worked but no artwork link was recorded | The account row has **Log Pixiv page details** (on by default). Keep it on, post once, and read `data\artdrop.log` — it lists every Pixiv call (method + path) and the key names that came back, which is what's needed to pin down the right endpoint. |
| "posted — the artwork is on Pixiv, but its link could not be read" | Pixiv accepted the work but its id could not be picked out of the page calls. The post **is** live (check your profile), the three-minute limit still applies, and the log file has the calls that were seen. |
| Nothing shows up in the Queue after dropping files | Check the banner says lazy mode is on (Queue tab → Save), that the folder path is the one you dropped into, and press **Scan folders now**. A fresh file waits ~30 s, and Caption AI must be set up (Accounts → Caption AI) — a batch with no key lands under *Needs you*. |
| Lazy mode says "Needs you" | Either the AI was not sure which character it is (caption it yourself and press Post), or it is the Pixiv entry, which always waits for your rating. |
| An entry says **AI unavailable** | The model timed out or was briefly down. ArtDrop retries by itself (after 1, 5 and 15 minutes) and only then asks you; press **Retry AI** to try immediately. **Scan folders now** also clears that waiting period. |
| A lazy drop did nothing for a minute | The AI call is still retrying in the background — the Queue tab's counts update as soon as it lands. |
| A drop with several characters posted them as separate sets | That is M6.1: loose files are split per character. To force one post instead, put the images in a **subfolder** (add a `caption.txt` there to write the caption yourself). |
| A mixed set got the wrong caption or the wrong characters | In the Queue tab press **Wrong? Move to Needs you** next to the set; it comes back as a draft you can fix (and the AI still adds the tags). |
| A lazy post failed | The item stays in the Queue with the error and a **Retry** button; your files stay in the outbox until every site of that batch is done. |
| Something in the outbox shows under **Stuck** | It is not an image ArtDrop can post (a PDF, PSD, ZIP…). Convert it, or move it out of the folder. |
| Secrets | Stored in Windows Credential Manager under the service name **ArtDrop**, one entry per site. Remove clears it. |

## 7. For developers

- Run tests: `python -m pytest` from the project root.
- The app is Python + FastAPI, bound to `127.0.0.1:8765` only. Writes require the
  `X-ArtDrop: 1` header; the `Host` header must be localhost (DNS-rebinding guard).
- Secrets live in the OS keyring, never on disk. Error messages are scrubbed of
  secret values before they reach the UI.
- Uploads are kept under `data/uploads/<post_id>/`, with a `status.json` recording
  each site's result (this is what powers the double-post guard). History is in
  `data/history.sqlite`.
- `run.py` starts the server and opens the browser; `Start ArtDrop.bat` handles the
  Windows virtual environment, including the fallback to Python 3.12, then 3.11.
  On every start it compares `requirements.txt` with `.venv\.requirements-hash`
  (`tools\check_requirements.py`) and re-runs pip only when they differ, then
  installs the Playwright browser when that marker is missing.
- Playwright lives on one dedicated thread (`app/core/browser.py:run()`), with one
  Playwright per process and one open context per profile; the lifespan exit closes
  it. Never call Playwright from anywhere else.
- The app writes `data\artdrop.log` (rotating: 1 MB × 3 backups, INFO and above)
  from the `artdrop.*` loggers. A scrubbing formatter redacts labelled secrets
  (`access_token=…`, `Bearer …`, `client_secret=…`) even inside tracebacks, and
  nothing logs request bodies or query strings.
- Lazy mode (M6) is three pieces: `app/core/lazy.py` (settings + folder scanning
  and batches), `app/core/queue.py` (`data/queue.sqlite`: items, slots, hashes,
  the identify cache) and `app/watch.py` (the background worker: scan → one
  Caption AI call per set → queue → post what is due, then move finished batches
  to `posted\`). The worker only posts through `app/main.py:publish_card`, the same path the Post
  tab uses, so the double-post guard and History are shared.
- Uploads older than 7 days are deleted on startup; the size limit (25 MB per
  image) is shared between the upload route and the image preparation code.
- M1 covers Bluesky, Telegram and Discord. M1.5 adds Caption AI (OpenCode Go;
  prompts and fallbacks in `app/core/captioner.py`; settings under the keyring
  entry "caption"). M2 adds the per-site caption styles, the shared OAuth helper
  (`app/core/oauth.py` + `app/platforms/oauth.py`, loopback callback and
  paste-code fallback) and the Tumblr and DeviantArt connectors; the per-image
  upload cap is now 32 MB. M3 adds Instagram with a swappable image host
  (`app/core/imagehost.py`, imgbb first). M5 adds Pixiv through Playwright
  (`app/core/browser.py` for the launcher, `app/platforms/pixiv_browser.py` for
  the form and its selectors). M6 folder "lazy mode" and the backlog queue.
- M4 (Reddit) is deferred by user decision (no Reddit account yet).
- Browser profiles live under `data/pixiv-profiles/<account key>/`; the launcher
  installs Playwright's Chromium once as a fallback and prefers installed
  Chrome/Edge.
- `Connector.publish` trims a post to the site's `max_images` and reports it in
  `Result.note` (shown on the card, in the dry-run preview and in History).
- Caption styles live in `data/settings.json` (non-secret) and are served by
  `GET/POST /api/settings`; a per-post style in `options[site].style` wins over
  the saved one.
- OAuth tokens are kept inside each site's keyring blob under `tokens`, and are
  refreshed automatically before they expire. DeviantArt's flow uses PKCE (S256),
  Instagram refreshes its 60-day token while a week is left, and Tumblr's uses
  neither.
- Multi-account sites (`MULTI_ACCOUNT_SITES` in `app/main.py`) keep their shared
  settings under the site's keyring entry and each account as `site:key`, with
  the key list in the shared entry (`app/core/accounts.py`). Card ids like
  `instagram:solo` flow through publish, the guard, History and the settings.
- Tumblr's traffic (token exchange, refresh, test, posts) uses the narrow TLS
  context from `app/core/net.py:tls_context` — api.tumblr.com drops the default
  OpenSSL ClientHello on some PCs; every other site keeps the default context.
