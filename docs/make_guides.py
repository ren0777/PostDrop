"""Build the two ArtDrop PDF guides (setup + user guide) with ReportLab.

Run from the project root on Windows (it uses fonts from the Windows Fonts folder):

    python -m pip install reportlab
    python docs/make_guides.py

The PDFs are written next to this file. Edit the text below and re-run after app changes.
"""
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (XPreformatted, KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

OUT = Path(__file__).resolve().parent
OUT.mkdir(parents=True, exist_ok=True)

FONTS = Path(r"C:\Windows\Fonts")
pdfmetrics.registerFont(TTFont("Body", str(FONTS / "arial.ttf")))
pdfmetrics.registerFont(TTFont("Body-Bold", str(FONTS / "arialbd.ttf")))
pdfmetrics.registerFont(TTFont("Body-Italic", str(FONTS / "ariali.ttf")))
pdfmetrics.registerFont(TTFont("Mono", str(FONTS / "consola.ttf")))
pdfmetrics.registerFont(TTFont("JP", str(FONTS / "YuGothM.ttc"), subfontIndex=0))
pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body-Italic",
                              boldItalic="Body-Bold")

ACCENT = colors.HexColor("#6D4AFF")
INK = colors.HexColor("#1F2330")
MUTED = colors.HexColor("#5B6070")
TIP_BG = colors.HexColor("#EEF0FF")
WARN_BG = colors.HexColor("#FFF4E5")
WARN_EDGE = colors.HexColor("#E8A33D")
RULE = colors.HexColor("#D9DCE6")
HEAD_BG = colors.HexColor("#F3F1FF")

S = {
    "title": ParagraphStyle("title", fontName="Body-Bold", fontSize=30, leading=36, textColor=INK),
    "subtitle": ParagraphStyle("subtitle", fontName="Body", fontSize=14, leading=20,
                               textColor=MUTED),
    "h1": ParagraphStyle("h1", fontName="Body-Bold", fontSize=18, leading=23, textColor=ACCENT,
                         spaceBefore=6, spaceAfter=8),
    "h2": ParagraphStyle("h2", fontName="Body-Bold", fontSize=13, leading=17, textColor=INK,
                         spaceBefore=10, spaceAfter=4),
    "body": ParagraphStyle("body", fontName="Body", fontSize=10.2, leading=15, textColor=INK,
                           alignment=TA_LEFT, spaceAfter=5),
    "small": ParagraphStyle("small", fontName="Body", fontSize=8.8, leading=12.5,
                            textColor=MUTED),
    "cell": ParagraphStyle("cell", fontName="Body", fontSize=9, leading=12.5, textColor=INK),
    "cellb": ParagraphStyle("cellb", fontName="Body-Bold", fontSize=9, leading=12.5,
                            textColor=INK),
    "code": ParagraphStyle("code", fontName="Mono", fontSize=9, leading=13, textColor=INK,
                           backColor=colors.HexColor("#F4F5F8"), borderPadding=(5, 6, 5, 6),
                           spaceBefore=3, spaceAfter=8),
    "box": ParagraphStyle("box", fontName="Body", fontSize=9.6, leading=14, textColor=INK),
}


def code_inline(text):
    return f'<font name="Mono" size="9">{text}</font>'


C = code_inline


def p(text, style="body"):
    return Paragraph(text, S[style])


def h1(text):
    return p(text, "h1")


def h2(text):
    return p(text, "h2")


def steps(items, start=1):
    return ListFlowable(
        [ListItem(p(item), leftIndent=16, value=start + index) for index, item in enumerate(items)],
        bulletType="1", bulletFontName="Body-Bold", bulletFontSize=10, leftIndent=16,
        bulletColor=ACCENT, spaceAfter=4,
    )


def bullets(items):
    return ListFlowable(
        [ListItem(p(item), leftIndent=14) for item in items],
        bulletType="bullet", start="\u2022", bulletFontName="Body", bulletFontSize=10,
        leftIndent=14, bulletColor=ACCENT, spaceAfter=4,
    )


def box(text, kind="tip"):
    label = {"tip": "Tip", "warn": "Important", "note": "Note"}[kind]
    bg = WARN_BG if kind == "warn" else TIP_BG
    edge = WARN_EDGE if kind == "warn" else ACCENT
    table = Table([[p(f"<b>{label}:</b> {text}", "box")]], colWidths=[170 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 3, edge),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return KeepTogether([Spacer(1, 3), table, Spacer(1, 7)])


def code(text):
    return XPreformatted(text, S["code"])


def table(rows, widths, header=True):
    data = [[p(str(cell), "cellb" if header and r == 0 else "cell") for cell in row]
            for r, row in enumerate(rows)]
    t = Table(data, colWidths=[w * mm for w in widths], repeatRows=1 if header else 0)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.5, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), HEAD_BG))
    t.setStyle(TableStyle(style))
    return KeepTogether([t, Spacer(1, 8)])


def cover(title, subtitle, blurb):
    return [
        Spacer(1, 55 * mm),
        p('<font color="#6D4AFF">Art</font>Drop', "title"),
        Spacer(1, 4),
        p(title, "h1"),
        p(subtitle, "subtitle"),
        Spacer(1, 12),
        Table([[""]], colWidths=[170 * mm], rowHeights=[2],
              style=[("BACKGROUND", (0, 0), (-1, -1), ACCENT)]),
        Spacer(1, 12),
        p(blurb),
        Spacer(1, 70 * mm),
        p("Open-source project. Works on Windows 10 and 11. Everything runs on your own PC.",
          "small"),
        PageBreak(),
    ]


def footer(label):
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Body", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(20 * mm, 12 * mm, f"ArtDrop - {label}")
        canvas.drawRightString(190 * mm, 12 * mm, f"Page {doc.page}")
        canvas.setStrokeColor(RULE)
        canvas.line(20 * mm, 16 * mm, 190 * mm, 16 * mm)
        canvas.restoreState()
    return draw


def build(filename, label, story, title):
    doc = SimpleDocTemplate(str(OUT / filename), pagesize=A4, leftMargin=20 * mm,
                            rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=22 * mm,
                            title=title, author="ArtDrop contributors",
                            subject=label)
    doc.build(story, onFirstPage=lambda c, d: None, onLaterPages=footer(label))
    return OUT / filename


# =================================================================== SETUP GUIDE
setup = cover(
    "Setup Guide",
    "Install ArtDrop and connect your art accounts",
    "ArtDrop posts your art to many sites at once. You drop in an image, it writes a caption "
    "for each site, and one click sends it to Bluesky, Telegram, Discord, Tumblr, DeviantArt, "
    "Instagram and Pixiv. This guide takes you from download to your first test post. Each "
    "site is set up once; after that you never repeat these steps.",
)

setup += [
    h1("1. What you need"),
    table([
        ["Item", "Details"],
        ["A Windows PC", "Windows 10 or 11. ArtDrop runs on your own computer; there is no cloud "
                         "server and no subscription."],
        ["Python 3.11 or newer", "Free from python.org. During install, tick <b>Add Python to "
                                 "PATH</b> and keep the <b>py launcher</b> option on."],
        ["Google Chrome", "Only needed for Pixiv (ArtDrop fills Pixiv's upload form in a real "
                          "browser window). Microsoft Edge works as a fallback."],
        ["Your art accounts", "Only the sites you want. Every site is optional."],
        ["Caption AI (optional)", "An OpenCode Go subscription, if you want the AI to look at "
                                  "your art and write captions and tags. ArtDrop works without "
                                  "it."],
        ["Disk space", "About 1 GB for ArtDrop, its Python packages and the browser component."],
    ], [45, 125]),
    box("ArtDrop only listens on your own PC (address 127.0.0.1). Your logins are stored in "
        "<b>Windows Credential Manager</b>, never in a plain file, and they are never shown "
        "back on the page. Nothing leaves your PC except the posts themselves.", "note"),

    h1("2. Install and start"),
    steps([
        "Download ArtDrop from its GitHub page: <b>Code &rarr; Download ZIP</b>, or clone it "
        f"with {C('git clone')}.",
        f"Extract it to a simple folder, for example {C('C:\\ArtDrop')}.",
        f"Double-click <b>{C('Start ArtDrop.bat')}</b>. The first start takes a few minutes: it "
        "creates a private Python environment and downloads what ArtDrop needs, including a "
        "browser component for Pixiv (about 150 MB). This happens only once.",
        f"Your browser opens <b>{C('http://127.0.0.1:8765/')}</b>. That page is ArtDrop.",
        "<b>Keep the black console window open</b> while you use ArtDrop. Closing it stops the "
        "app.",
    ]),
    box("If Windows asks whether to let Python through the firewall, you can press Cancel. "
        "ArtDrop does not accept connections from other computers.", "tip"),
    box("When you download a newer ArtDrop later, start it with the same .bat file. It notices "
        "new requirements and prints <i>Updating ArtDrop's components...</i> before starting. "
        f"Keep your old {C('data')} folder (copy it into the new version) to keep your queue, "
        "history and Pixiv logins.", "tip"),

    h1("3. Connect your sites"),
    p("Open the <b>Accounts</b> tab. Every site has a card with <b>Save</b>, <b>Test "
      "connection</b> and <b>Remove</b>. Set up only the sites you use. After each one, press "
      "<b>Test connection</b>: a green message means it works."),

    h2("Bluesky (about 2 minutes)"),
    steps([
        "In Bluesky, open <b>Settings &rarr; Privacy and security &rarr; App passwords &rarr; "
        "Add App Password</b>. Name it ArtDrop and leave direct-message access off.",
        "Copy the password it shows (it looks like xxxx-xxxx-xxxx-xxxx). It is shown once.",
        "In ArtDrop, Bluesky card: enter your <b>handle</b> (yourname.bsky.social, without @) "
        "and the <b>app password</b>. Save, then Test connection.",
    ]),
    box("Never enter your real Bluesky password. An app password can be revoked any time.",
        "warn"),

    h2("Telegram (about 3 minutes)"),
    steps([
        "In Telegram, message <b>@BotFather</b> (blue checkmark), send /newbot and follow the "
        "steps. The username must end in <i>bot</i>.",
        "BotFather sends a <b>token</b> (123456789:AA...). Tap it to copy the whole token.",
        "Open your channel &rarr; <b>Manage channel &rarr; Administrators &rarr; Add admin</b>, "
        "add your bot and keep <b>Post messages</b> on.",
        "In ArtDrop, Telegram card: paste the <b>bot token</b>. As <b>chat id</b>, enter your "
        "channel's @username for a public channel. For a private channel, use its numeric id "
        "(it starts with -100).",
    ]),
    box("\"Unauthorized\" on Test connection means the token was copied incompletely or was "
        "replaced. In BotFather send /mybots, pick your bot, tap API Token and copy it again.",
        "tip"),

    h2("Discord (about 1 minute)"),
    steps([
        "In Discord, right-click your art channel &rarr; <b>Edit Channel &rarr; Integrations "
        "&rarr; Webhooks &rarr; New Webhook &rarr; Copy Webhook URL</b>.",
        "In ArtDrop, Discord card: paste the webhook URL. Save, then Test connection.",
    ]),
    box("Treat the webhook URL like a password: anyone who has it can post in that channel. "
        "If it leaks, delete the webhook in Discord and make a new one.", "warn"),

    h2("Tumblr (about 5 minutes)"),
    steps([
        f"Open {C('tumblr.com/oauth/apps')} &rarr; <b>Register application</b>. Any name, "
        f"description and website (for example {C('https://example.com')}).",
        f"In both <b>Default callback URL</b> and <b>OAuth2 redirect URLs</b> enter exactly "
        f"{C('http://localhost:8765/oauth/tumblr')}. Tumblr refuses the address 127.0.0.1, so "
        "use the word localhost.",
        "Register, then copy the app's <b>OAuth Consumer Key</b> (client ID) and "
        "<b>Secret</b>.",
        "In ArtDrop, Tumblr card: your <b>blog</b> (yourname.tumblr.com), the client ID, the "
        f"client secret, and as <b>Redirect URI</b> the same {C('http://localhost:8765/oauth/tumblr')}. "
        "Save.",
        "Press <b>Connect</b> &rarr; <b>Allow</b>. The page says <i>connected</i>. Then Test "
        "connection.",
    ]),

    h2("DeviantArt (about 5 minutes)"),
    steps([
        f"Open {C('deviantart.com/developers/apps')} &rarr; <b>Register Application</b>.",
        f"<b>OAuth2 Redirect URI Whitelist:</b> {C('https://127.0.0.1/artdrop')}. DeviantArt only "
        "accepts https addresses.",
        "<b>Client type:</b> Confidential. Leave the other fields empty, accept the terms and "
        "save. Copy the <b>client_id</b> and <b>client_secret</b>.",
        f"In ArtDrop, DeviantArt card: the client ID and secret, and as <b>Redirect URI</b> "
        f"{C('https://127.0.0.1/artdrop')}. Save.",
        "Press <b>Connect</b> &rarr; <b>Authorize</b>. Your browser then shows <i>This site "
        "can't be reached</i>. <b>That is expected.</b>",
        "Right away, copy the <b>whole address</b> from the address bar, paste it into the code "
        "box on the DeviantArt card and press <b>Finish connection</b>.",
    ]),
    box("DeviantArt login codes work once and expire within minutes. If you see \"Incorrect "
        "authorization code\", press Connect again and paste the newest address.", "tip"),

    h2("Instagram (about 20 to 30 minutes, once)"),
    p("Instagram needs a <b>Professional</b> account (Creator or Business). Switching is free "
      "in the Instagram app under <b>Settings &rarr; Account type and tools</b>. No Facebook "
      "Page is needed and your Instagram does not have to be linked to Facebook."),
    steps([
        f"Go to {C('developers.facebook.com')} and log in with any Facebook account. It only "
        "owns the developer app. Nothing is ever posted to Facebook.",
        "<b>My Apps &rarr; Create App</b>. Choose the use case <b>Manage messaging &amp; content "
        "on Instagram</b>, and <i>I don't want to connect a business portfolio yet</i>.",
        "Open <b>Customize &rarr; Permissions and features</b> and add only "
        f"{C('instagram_business_basic')} and {C('instagram_business_content_publish')}. Skip "
        "the \"Add all required permissions\" button (those are messaging permissions).",
        "Open <b>API setup with Instagram login &rarr; Generate access tokens &rarr; Add "
        "account</b> and log in as the Instagram account you want to post from. Use a private "
        "browser window for each extra account, so the right one is used.",
        "In <b>Set up Instagram business login &rarr; Business login settings</b>, add the "
        f"redirect URI {C('https://localhost/artdrop')} <b>with no slash at the end</b>, and "
        "save.",
        "Copy the <b>Instagram app ID</b> and <b>Instagram app secret</b> from the API setup "
        "page (not the Meta app ID at the top).",
        f"Get a free image-host key: sign in at {C('api.imgbb.com')} and press <b>Get API "
        "key</b>. Instagram fetches pictures from a public link, so ArtDrop parks each image "
        "there for one hour.",
        "In ArtDrop, Instagram card: app ID, app secret, Redirect URI "
        f"{C('https://localhost/artdrop')} and the imgbb key. Save.",
        "Press <b>Connect &rarr; Allow</b>. On the <i>can't be reached</i> page, copy the whole "
        "address, paste it into the code box and press <b>Finish connection</b>.",
    ]),
    box("Do not use the \"Instagram Tester\" role under App roles. It belongs to a retired API "
        "and its form will not save. Accounts are added with <b>Add account</b> as above. "
        "Accounts with an active Instagram restriction may be refused until the restriction "
        "ends.", "warn"),
    p("<b>More than one Instagram account:</b> add each one to the Meta app (step 4), then "
      "press <b>Add Instagram account</b> in ArtDrop, give it a name (for example Solo or "
      "Collab) and connect it. On an account that should invite collaborators, tick <b>Can "
      "send collab invites</b> and enter up to three usernames. The invited account accepts "
      "the invite in the Instagram app."),
    p("ArtDrop renews Instagram's login by itself every 60 days."),

    h2("Pixiv (about 5 minutes per account)"),
    p("Pixiv has no official posting API. ArtDrop fills Pixiv's normal upload form in a real "
      "Chrome window and <b>you press Post yourself</b>. ArtDrop never sees your Pixiv "
      "password."),
    steps([
        "Accounts tab &rarr; <b>Add Pixiv account</b>, give it a name, press <b>Log in</b>.",
        "A normal Chrome window opens on Pixiv's login page. Sign in, including any CAPTCHA "
        "or two-factor code.",
        "When you see your Pixiv home page, <b>close that Chrome window</b>.",
        "Press <b>Check login</b>. It says <i>Logged in to Pixiv</i>.",
        "For a second account, repeat with <b>Add Pixiv account</b>.",
    ]),
    box("If you normally log in to Pixiv with Google, Google may block the sign-in in this "
        "window. Set a Pixiv password first (pixiv.net &rarr; Settings &rarr; Account &rarr; "
        "Password) and sign in with email and password.", "tip"),
    box("Pixiv's automation is unofficial. ArtDrop keeps it safe by stopping before Submit, "
        "always marking works as AI-generated, and never posting to Pixiv in bulk. Pixiv could "
        "still change its page; ArtDrop then tells you which field to fill by hand.", "note"),

    h2("Caption AI (optional)"),
    steps([
        f"Sign in at {C('opencode.ai')}, open the Console and create an <b>API key</b> for your "
        "Go subscription.",
        "In ArtDrop, open <b>Accounts &rarr; Caption AI</b>, paste the key and Save. Leave the "
        "model boxes as they are.",
        "Press <b>Test connection</b>. It checks that both models can really see images, for "
        "example: <i>Caption AI ready - vision deepseek-v4.1-flash, backup mimo-v2.6-flash</i>.",
    ]),
    box("AI models get retired now and then. If Test connection says <i>model not available "
        "(retired or renamed)</i>, enter another image-capable model from the OpenCode Go list "
        "(for example mimo-v2.6-flash or glm-5.3-flash). No code change is needed.", "tip"),

    h2("Not supported yet"),
    p("<b>X / Twitter</b> (its API is pay-per-post) and <b>Reddit</b> (new API apps need "
      "approval, and new accounts are filtered by most art subreddits). Both can be added "
      "later."),

    h1("4. Your first test post"),
    steps([
        "Open the <b>Post</b> tab and drop in one finished piece.",
        "Tick the sites you connected. On Tumblr you can tick <b>Post as a draft</b>, and on "
        "DeviantArt <b>Save to Sta.sh only</b>, for a private first test.",
        "Tick <b>Dry run</b> and press <b>Post</b>. Each card shows exactly what would be sent. "
        "Nothing is posted.",
        "Untick Dry run and press <b>Post</b>. Each card turns to <i>posted</i> with a link.",
    ]),
    p("From here, the <b>User Guide</b> explains everything else: captions, Pixiv, Instagram "
      "collabs and the automatic folder (lazy) mode."),

    h1("5. Troubleshooting"),
    table([
        ["Problem", "What to do"],
        ["The page says it can't be reached", "Check that the black console window is still "
                                              "open, then refresh the page."],
        ["\"Address already in use\"", "ArtDrop is already running. Just open "
                                       "http://127.0.0.1:8765/."],
        ["\"Not set up yet - missing: ...\"", "Open Accounts, fill in the named fields, Save."],
        ["Bluesky login failed", "Use an app password, not your account password."],
        ["Telegram getChat failed", "The bot is not an admin of the channel, or the chat id is "
                                    "wrong."],
        ["\"Invalid redirect_uri\" (Instagram)", "The Redirect URI in ArtDrop and in Meta's "
                                                "Business login settings must match exactly: "
                                                "https://localhost/artdrop with no slash."],
        ["\"This url's domain is not valid\" (Tumblr)", "Use localhost instead of 127.0.0.1."],
        ["Pixiv keeps showing \"Verify you are human\"", "That window is automated. Use "
                                                        "<b>Log in</b> on the Pixiv card, which "
                                                        "opens a normal Chrome."],
        ["Pixiv says a Chrome window still has the account open", "Close the Pixiv Chrome "
                                                                  "window, then press the "
                                                                  "button again."],
        ["The page looks outdated after an update", "Press Ctrl+F5 to reload it."],
        ["Something else", f"Look in {C('data\\artdrop.log')}. It records what happened "
                           "without ever writing your passwords or tokens."],
    ], [60, 110]),
]

# ==================================================================== USER GUIDE
guide = cover(
    "User Guide",
    "Post once, reach every site",
    "This guide shows how to use ArtDrop day to day: posting a piece by hand, letting the AI "
    "write captions for every site, posting to Pixiv and Instagram, and the lazy mode that "
    "posts from a folder by itself. It assumes your sites are already connected (see the "
    "Setup Guide).",
)

guide += [
    h1("1. The four tabs"),
    table([
        ["Tab", "What it is for"],
        ["Post", "Post a piece by hand: images, character, captions, sites, then Post."],
        ["Queue", "Lazy mode: the folder settings, the upcoming posts, and the <b>Needs you</b> "
                  "list."],
        ["Accounts", "Connect and test your sites and the Caption AI."],
        ["History", "Every post and every attempt, with a link to each real post."],
    ], [30, 140]),

    h1("2. Posting a piece"),
    steps([
        "<b>New post</b> clears the page. Drop in your images; the first image is the cover. "
        "You can also drop a .txt file with the caption, and a line starting with "
        f"{C('tags:')} for tags.",
        "Type the <b>character</b> (and the <b>series</b> if you like). Tick <b>Mature</b> for "
        "suggestive pieces.",
        "Tick the sites to post to. Each site has its own card.",
        "Press <b>Generate</b>. The AI looks at the cover image and writes a caption, tags and "
        "alt text for each ticked site, in that site's style.",
        "Read and edit anything. Everything stays editable, and nothing posts by itself.",
        "Optional: tick <b>Dry run</b> and press Post to preview. Then untick it.",
        "Press <b>Post</b>. Each card shows its own result and a link.",
    ]),
    p("If one site fails, the others still go out. A failed card gets a <b>Retry</b> button. A "
      "card that shows <i>posted</i> is locked, and pressing Post again only sends to sites "
      "that don't have the post yet. So nothing is posted twice by accident."),
    box("If you type your own caption before pressing Generate, the AI <b>polishes</b> it for "
        "each site and keeps your meaning. Tick <b>Use my caption as-is</b> to post your words "
        "exactly.", "tip"),

    h2("Caption styles"),
    p("Every site card has a <b>Caption style</b> menu:"),
    table([
        ["Style", "What gets posted"],
        ["AI caption", "The caption and tags written for that site (or your own)."],
        ["Name + series", "Only the character and series, for example: Stelle - Honkai: Star "
                          "Rail. No description and no tags. The default for Telegram."],
        ["Images only", "No text at all."],
    ], [35, 135]),

    h2("What the AI writes for each site"),
    table([
        ["Site", "Caption", "Tags"],
        ["Bluesky", "one or two short lines, 300 characters in total", "3 to 5"],
        ["Telegram", "2 to 4 warmer lines (or Name + series)", "6 to 10"],
        ["Discord", "one casual line", "0 to 3"],
        ["Tumblr", "1 to 3 lines", "5 to 15, in Tumblr's tag field"],
        ["DeviantArt", "a short title and a 2 to 4 line description", "up to 30"],
        ["Instagram", "1 to 3 friendly lines", "10 to 15 hashtags"],
        ["Pixiv", "a Japanese title and caption", "up to 10, in Japanese, with the "
                                                  "series and official character name first"],
    ], [30, 80, 60]),
    p("The AI never mentions AI, prompts or tools in captions. Sites that require an AI label "
      "get it through their own setting instead (DeviantArt's <i>Created using AI tools</i>, "
      "Pixiv's <i>AI-generated work</i>). A small #aiart tag is added by default; you can "
      "switch it off in <b>Accounts &rarr; Caption AI</b>."),

    h2("Images per site"),
    p("If you post more images than a site allows, ArtDrop sends the first ones and says so on "
      "the card, for example <i>posted the first 4 of 10 images</i>. Put your best image "
      "first."),
    table([
        ["Site", "Images per post", "Notes"],
        ["Bluesky", "4", "shrunk to about 1 MB each"],
        ["Telegram, Discord, Tumblr", "10", ""],
        ["DeviantArt", "1", "the full-size original is sent"],
        ["Instagram", "10 (carousel)", "tall art is fitted to 4:5 with white bars at the "
                                       "sides"],
        ["Pixiv", "many", "originals up to 32 MB; WEBP becomes PNG"],
    ], [45, 35, 90]),

    h1("3. Site notes"),
    h2("DeviantArt"),
    bullets([
        "Mature pieces are marked mature with the level you choose (moderate or strict).",
        "Every deviation is labelled <i>Created using AI tools</i> and <i>NoAI</i> (not for AI "
        "training), as DeviantArt's rules require.",
        "The title comes from the AI (for example <i>Stelle at the Summer Festival</i>). You "
        "can type your own on the card.",
    ]),
    h2("Tumblr"),
    bullets([
        "Tumblr's API can't set the <i>Mature</i> community label. For suggestive pieces, set "
        "it in the Tumblr app after posting; ArtDrop reminds you.",
    ]),
    h2("Instagram"),
    bullets([
        "Instagram is much stricter than the other sites about suggestive art. Post your safer "
        "pieces there.",
        "On an account with collab invites on, the card shows <i>Will invite: @name</i> under "
        "the Collaborators box. Clear the box to post without a collaborator this time.",
        "The invited account must accept the invite in the Instagram app. Then the post shows "
        "on both profiles.",
    ]),
    h2("Pixiv (semi-automatic)"),
    steps([
        "Tick one or more Pixiv accounts and pick the <b>rating</b> on each card: All ages, "
        "R-18 or R-18G. ArtDrop never picks the rating for you.",
        "Press Post. A Chrome window opens and fills in the images, Japanese title, caption, "
        "tags, the rating and <i>AI-generated work: Yes</i>. Then it stops.",
        "Check the form. Fix anything you like directly in the window.",
        "Press <b>Post</b> in the Pixiv window yourself. ArtDrop saves the artwork link, and "
        "the next account's window opens.",
    ]),
    box("Leave <i>Original work</i> unticked for fan art. Tick it yourself in the Pixiv window "
        "only for your own original characters.", "tip"),
    p('Japanese example of a title the AI may write: <font name="JP">推しはどの子？</font> '
      '("Which one is your favourite?")'),

    h1("4. Lazy mode: post from a folder"),
    p("Lazy mode posts by itself. You drop images into a folder, and ArtDrop captions them and "
      "posts them over time, spaced out so your feed looks natural."),
    h2("Turning it on"),
    steps([
        "Open the <b>Queue</b> tab and tick <b>Post from my outbox folders "
        "automatically</b>, then Save. A banner shows while lazy mode is on, with a "
        "<b>Pause</b> button.",
        "Check which sites each folder feeds (the tick boxes under each folder).",
    ]),
    table([
        ["Folder", "Posts to", "Mature"],
        [C("Pictures\\ArtDrop\\outbox"), "Bluesky, Telegram, Discord, Tumblr, DeviantArt",
         "yes, by default"],
        [C("Pictures\\ArtDrop\\outbox-instagram"), "the same sites plus Instagram", "no"],
    ], [72, 70, 28]),
    box("Lazy mode really posts in public without asking you. Only put pieces in these folders "
        "that you are happy to have posted as they are.", "warn"),

    h2("How images become posts: one subfolder = one post"),
    p("The simplest and most reliable way is to use <b>subfolders</b> and put nothing loose in "
      "the outbox. The folder name tells ArtDrop what the post is, so the AI never has to guess "
      "who the character is. It only writes the captions."),
    table([
        ["You want", "Do this", "Result"],
        ["One character", f"{C('outbox\\Changli\\')} with the images",
         "one post about <b>Changli</b>. The AI adds the game if it knows it."],
        ["The same character again", f"{C('outbox\\Changli (2)\\')}",
         "another Changli post. A counter like (2), _2 or -2 is ignored."],
        ["Several characters in one post", f"{C('outbox\\mix\\')} or {C('mix-morning')}",
         "one post with a catchy universal caption such as <i>Good morning! Which one is "
         "your fav? Comment below!</i>"],
        ["Your own caption", f"a {C('caption.txt')} inside the subfolder",
         f"the AI polishes your words for each site. First line {C('!exact')} = use them "
         "word for word."],
        ["A reminder folder", f"a name starting with {C('_')}",
         "never posted (for example the empty _EXAMPLE Character Name folder)."],
    ], [38, 55, 77]),
    code("Pictures\\ArtDrop\\\n"
         "    outbox\\                       mature pieces\n"
         "        Changli\\       1.png 2.png          -> one post: Changli\n"
         "        Changli (2)\\   3.png                -> another Changli post\n"
         "        mix-morning\\   a.png b.png c.png    -> one universal post\n"
         '                       caption.txt  "good morning, which one is your fav?"\n'
         "    outbox-instagram\\             safe pieces (also go to Instagram)\n"
         "        Jane Doe\\      1.png\n"
         "    posted\\2026-10-01\\Changli\\    <- finished sets end up here, with their links"),
    p("<b>Loose images</b> (dropped directly into an outbox folder) also work: characters the "
      "AI recognises become one post each, and images it doesn't recognise are posted together "
      "as one <b>universal</b> post with a catchy caption."),
    box("A name that really ends in a number stays intact: <i>Android 18</i> is posted as "
        "Android 18. Use a counter in brackets, like <i>Changli (2)</i>, for repeat posts.",
        "tip"),

    h2("The schedule"),
    p("Every site has its own queue. Sites that take many images get bundles; sites that take "
      "few get one post at a time. The defaults (changeable under <b>Per-site images and "
      "spacing</b>, never below 30 minutes):"),
    table([
        ["Site", "Images per post", "Time between posts"],
        ["Telegram, Discord, Tumblr, Instagram", "up to 10", "2 hours"],
        ["Bluesky", "4", "2 hours"],
        ["DeviantArt", "1", "2 hours"],
    ], [75, 45, 50]),
    p("Example: a set of 20 images becomes 2 posts of 10 on Telegram, 5 posts of 4 on Bluesky "
      "and 20 posts on DeviantArt, each site spaced 2 hours apart. Optional <b>quiet "
      "hours</b> hold posts during the night."),
    box("Posts go out only while ArtDrop is open and the PC is awake. If the PC was off, "
        "nothing is lost: overdue posts continue one at a time with the normal spacing, never "
        "all at once. To keep posting while you're away, leave ArtDrop running and set Windows "
        "sleep to a longer time.", "note"),

    h2("Needs you"),
    bullets([
        "<b>Pixiv</b> waits here for every set, because you pick the rating and press Post. One "
        "click opens the Post tab with the Japanese text filled in. If you don't want a set on "
        "Pixiv, press <b>Dismiss</b>. To skip Pixiv for a whole folder, untick <b>Also prepare "
        "for Pixiv</b> in that folder's settings.",
        "If the AI service is down, ArtDrop retries a few times by itself. Only then does the "
        "set land here, with a <b>Retry AI</b> button.",
        "A queued set with the wrong text can be pulled back with <b>Wrong? Move to Needs "
        "you</b>.",
    ]),
    h2("Queue controls"),
    p("<b>Post now</b>, <b>Edit caption</b>, <b>Skip</b>, <b>Retry</b>, <b>Remove</b> and "
      "<b>Pause lazy mode</b>. A <b>Stuck</b> list shows files ArtDrop can't post (such as a "
      "PSD or a ZIP)."),
    h2("After posting"),
    p("Images stay in the outbox until the <b>last</b> post of their set has gone out (and the "
      "Pixiv entry is posted or dismissed). Then the whole set moves to "
      f"{C('Pictures\\ArtDrop\\posted\\&lt;date&gt;\\&lt;folder name&gt;')} with a small file of "
      "all its post links, and the emptied folder is removed from the outbox."),
    box("Each image is posted at most once per site. ArtDrop recognises images by their "
        "content, so renaming a file does not post it again, and a new image with an old file "
        "name is still picked up.", "tip"),

    h1("5. History"),
    p("The History tab lists every attempt: time, site, result, a link to the post, and the "
      "error if something failed. Dry runs are listed too, marked as such."),

    h1("6. Good habits"),
    bullets([
        "Mark suggestive pieces as <b>Mature</b>, and pick the right Pixiv rating. Wrong ratings "
        "can get accounts restricted.",
        "Respect each site's rules on AI art. ArtDrop sets the AI labels, but communities and "
        "groups may have their own rules.",
        "Keep your keys and webhook links private. If one leaks, revoke it on the site and "
        "enter a new one in Accounts.",
        "Use Dry run whenever you change something big.",
    ]),
]

for path in (
    build("ArtDrop-Setup-Guide.pdf", "Setup Guide", setup, "ArtDrop - Setup Guide"),
    build("ArtDrop-User-Guide.pdf", "User Guide", guide, "ArtDrop - User Guide"),
):
    print(path)
