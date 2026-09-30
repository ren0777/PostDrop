"""Guards for the frontend templates the live reviews found wanting.

There is no JavaScript test runner here, so these check the exact fragments in
the templates — plus one rendered check through node when it is installed.
"""
import json
import shutil
import subprocess
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
STYLE = (WEB / "style.css").read_text(encoding="utf-8")

NODE = shutil.which("node")

# Loads app.js in node with a stub DOM and renders one option field, so the real
# template is exercised instead of a copy of it.
RENDER_HARNESS = r"""
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8").replace(/init\(\);\s*$/, "");
const field = JSON.parse(process.argv[2]);
global.document = {
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementById: () => null,
  addEventListener: () => {},
  createElement: () => ({ style: {} }),
};
global.window = { addEventListener: () => {}, confirm: () => true, prompt: () => "" };
global.localStorage = { getItem: () => null, setItem: () => {} };
global.URL = { createObjectURL: () => "" };
global.fetch = () => Promise.reject(new Error("no network in tests"));
console.log(new Function(source + "\nreturn optionFieldHtml;")()({ id: "instagram:main" }, field));
"""


def render_option_field(field: dict) -> str:
    result = subprocess.run(
        [NODE, "-e", RENDER_HARNESS, str(WEB / "app.js"), json.dumps(field)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


# Drives one account-row action in node with a stub DOM/fetch, so the buttons'
# wiring is exercised instead of only the template text.
ROW_HARNESS = r"""
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8").replace(/init\(\);\s*$/, "");
const action = process.argv[2];
const cardId = process.argv[3];
const nodes = {};
const warnings = [];
console.warn = (...args) => warnings.push(args.join(" "));
global.document = {
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementById: (id) => (nodes[id] = nodes[id] || { textContent: "", className: "" }),
  addEventListener: () => {},
  createElement: () => ({ style: {} }),
};
global.window = { addEventListener: () => {}, confirm: () => true, prompt: () => "" };
global.localStorage = { getItem: () => null, setItem: () => {} };
global.URL = { createObjectURL: () => "" };
const calls = [];
global.fetch = async (url) => {
  calls.push(String(url));
  return { ok: true, status: 200,
           json: async () => ({ ok: true, detail: "Logged in to Pixiv." }) };
};
const app = new Function(source + `
  loadPlatforms = async () => {};
  renderAccounts = () => {};
  renderCards = () => {};
  return { accountRowAction, state };
`)();
app.state.accountSections = {
  pixiv: { flow: "browser", accounts: [{ id: "pixiv:art" }] },
  instagram: { flow: "oauth", accounts: [{ id: "instagram:main" }] },
};
(async () => {
  await app.accountRowAction(action, cardId);
  const node = nodes["result-" + cardId] || {};
  console.log(JSON.stringify({ calls, warnings, text: node.textContent,
                               className: node.className }));
})();
"""


def row_action(action: str, card_id: str = "pixiv:art") -> dict:
    result = subprocess.run(
        [NODE, "-e", ROW_HARNESS, str(WEB / "app.js"), action, card_id],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# Runs publishOne() in node and captures the request body it would send, so the
# publish options (the rating row especially) are exercised end to end.
PUBLISH_HARNESS = r"""
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8").replace(/init\(\);\s*$/, "");
const dryRun = process.argv[2] === "dry";
const nodes = {};
const single = {
  "#caption": { value: "星の夜" },
  "#tags": { value: "" },
  "#alt": { value: "" },
  "#character": { value: "星" },
  "#series": { value: "崩壊：スターレイル" },
  "#mature": { checked: false },
};
global.document = {
  querySelector: (selector) => single[selector] || null,
  querySelectorAll: () => [],
  getElementById: (id) => (nodes[id] = nodes[id] || { value: "", checked: false,
                                                      textContent: "", className: "" }),
  addEventListener: () => {},
  createElement: () => ({ style: {} }),
};
global.window = { addEventListener: () => {}, confirm: () => true, prompt: () => "" };
global.localStorage = { getItem: () => null, setItem: () => {} };
global.URL = { createObjectURL: () => "" };
const bodies = [];
global.fetch = async (url, options) => {
  bodies.push({ url: String(url),
                body: options && options.body ? JSON.parse(options.body) : null });
  return { ok: true, status: 200, json: async () => ({ ok: true, detail: "ok" }) };
};
const app = new Function(source + `
  setCard = () => {};
  return { publishOne, state };
`)();
app.state.ratings = { "pixiv:art": "r18" };
app.state.styles = {};
(async () => {
  await app.publishOne({
    id: "pixiv:art",
    option_fields: [
      { key: "title", kind: "text" },
      { key: "rating", kind: "rating" },
    ],
  }, "post-1", dryRun);
  console.log(JSON.stringify(bodies));
})();
"""


def publish_body(dry_run: bool) -> dict:
    result = subprocess.run(
        [NODE, "-e", PUBLISH_HARNESS, str(WEB / "app.js"), "dry" if dry_run else "post"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    bodies = json.loads(result.stdout)
    published = [entry for entry in bodies if entry["url"] == "/api/publish"]
    assert published, bodies
    return published[0]["body"]


# Renders the lazy banner for one on/off state, so the `hidden` attribute it sets
# is actually exercised (its CSS used to override the attribute).
BANNER_HARNESS = r"""
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8").replace(/init\(\);\s*$/, "");
const enabled = process.argv[2] === "on";
const needs = Number(process.argv[3] || 0);
const nodes = {};
const node = (id) => (nodes[id] = nodes[id] || { textContent: "", className: "", hidden: false,
                                                 checked: false, value: "" });
global.document = {
  querySelector: (selector) => node(selector),
  querySelectorAll: () => [],
  getElementById: (id) => node(id),
  addEventListener: () => {},
  createElement: () => ({ style: {} }),
};
global.window = { addEventListener: () => {}, confirm: () => true, prompt: () => "" };
global.localStorage = { getItem: () => null, setItem: () => {} };
global.URL = { createObjectURL: () => "" };
global.fetch = () => Promise.reject(new Error("no network in tests"));
const app = new Function(source + "\nreturn { state, renderLazyBanner };")();
app.state.lazy = { settings: { enabled, paused: false } };
app.state.queue = { queue: { counts: { pending: 2, failed: 0, needs_you: needs } } };
app.renderLazyBanner();
console.log(JSON.stringify({
  hidden: !!nodes["#lazy-banner"].hidden,
  badgeHidden: !!nodes["#queue-badge"].hidden,
  badge: nodes["#queue-badge"].textContent,
  labels: (nodes["#lazy-pause"] || {}).textContent || "",
}));
"""


def banner_state(enabled: bool, needs_you: int = 0) -> dict:
    result = subprocess.run(
        [NODE, "-e", BANNER_HARNESS, str(WEB / "app.js"), "on" if enabled else "off",
         str(needs_you)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_card_option_inputs_prefill_from_their_value():
    assert 'value="${escapeHtml(field.value || "")}"' in APP_JS


def test_free_text_inputs_disable_autofill():
    # the card's own inputs
    assert APP_JS.count('autocomplete="off"') >= 4
    assert 'name="artdrop-override-${platform.id}"' in APP_JS
    assert 'name="artdrop-tags-${platform.id}"' in APP_JS
    # the Caption AI box
    for anchor in ('name="artdrop-character"', 'name="artdrop-series"',
                   'name="artdrop-notes"'):
        assert anchor in INDEX
        assert 'autocomplete="off"' in INDEX


def test_the_collaborator_field_shows_a_live_invite_line():
    assert "function inviteText(" in APP_JS
    assert "Will invite:" in APP_JS
    assert "No collaborators on this post." in APP_JS
    assert 'id="invite-${platform.id}"' in APP_JS
    assert 'data-invite="${escapeHtml(platform.id)}"' in APP_JS


def test_the_dry_run_shows_who_will_be_invited():
    assert "preview.collaborators" in APP_JS
    assert "will invite" in APP_JS


def test_an_empty_option_box_is_still_sent():
    # the per-post value must win over the account default, even when empty
    assert "site[field.key] = node.value.trim();" in APP_JS


def test_checkbox_options_render_checked_from_their_value():
    # a stored True (collab invites, full auto) must come back ticked, or saving
    # the row would silently post `false`
    assert '${field.value ? "checked" : ""}' in APP_JS


def test_a_rendered_checkbox_follows_its_value():
    if NODE is None:
        return  # node is not installed here; the fragment guard above still runs
    ticked = render_option_field({"key": "can_invite", "label": "Can send collab invites",
                                  "kind": "checkbox", "value": True})
    clear = render_option_field({"key": "draft", "label": "Save as draft",
                                 "kind": "checkbox", "value": ""})
    assert "checked" in ticked
    assert "checked" not in clear


def test_adding_an_account_routes_through_the_card_action():
    # a browser-flow account (Pixiv) must not be sent to the OAuth endpoint
    assert 'await accountRowAction("connect", response.id);' in APP_JS


def test_the_browser_flow_says_check_login():
    # Pixiv signs in with the user's own Chrome, so the verify button is renamed
    assert 'section.flow === "browser" ? "Check login" : "Test connection"' in APP_JS


def test_per_account_rows_call_the_test_endpoint():
    # account rows are handled by accountRowAction, which has its own test path
    assert 'await api(`/api/test/${cardId}`, { method: "POST" });' in APP_JS


def test_a_row_check_login_actually_calls_the_test_endpoint():
    if NODE is None:
        return
    result = row_action("test")
    assert result["calls"] == ["/api/test/pixiv:art"]
    assert result["text"] == "Logged in to Pixiv."
    assert result["className"] == "result ok"


def test_an_instagram_row_check_connection_calls_the_test_endpoint():
    if NODE is None:
        return
    result = row_action("test", "instagram:main")
    assert result["calls"] == ["/api/test/instagram:main"]


def test_a_row_check_login_refreshes_the_card_state():
    if NODE is None:
        return
    # loadPlatforms/renderAccounts/renderCards run (stubbed out in the harness),
    # so the badge can turn "logged in" right after the check
    assert "await loadPlatforms();" in APP_JS.split("if (action === \"test\")")[1][:600]


def test_an_unknown_row_action_warns_instead_of_doing_nothing():
    if NODE is None:
        return
    result = row_action("nonsense")
    assert result["calls"] == []
    assert any("unknown action" in warning for warning in result["warnings"])


def test_an_unknown_action_on_a_single_account_card_warns():
    if NODE is None:
        return
    assert "console.warn(\"ArtDrop: unknown action\"" in APP_JS
    assert APP_JS.count("console.warn(\"ArtDrop: unknown action\"") >= 3  # rows, oauth, cards


def test_the_rating_field_is_read_before_the_option_node_lookup():
    # the rating row has no opt-… node, so reading it after the `!node` return
    # is what swallowed the choice
    loop = APP_JS.split("(platform.option_fields || []).forEach")[1]
    assert loop.index('field.kind === "rating"') < loop.index("if (!node) return;")


def test_the_chosen_rating_reaches_the_publish_body():
    if NODE is None:
        return
    body = publish_body(dry_run=False)
    assert body["options"]["pixiv:art"]["rating"] == "r18"
    assert body["dry_run"] is False


def test_the_chosen_rating_reaches_the_dry_run_body():
    if NODE is None:
        return
    body = publish_body(dry_run=True)
    assert body["options"]["pixiv:art"]["rating"] == "r18"
    assert body["dry_run"] is True


def test_a_hidden_attribute_always_hides():
    # the lazy banner's own display:flex used to win over the attribute
    assert "[hidden] { display: none !important; }" in STYLE


def test_the_lazy_banner_is_hidden_while_lazy_mode_is_off():
    if NODE is None:
        return
    off = banner_state(False, needs_you=1)
    assert off["hidden"] is True  # not shown while lazy mode is off…
    assert off["badgeHidden"] is False and off["badge"] == "1"  # …but the tab still nags

    on = banner_state(True)
    assert on["hidden"] is False
    assert on["labels"] == "Pause"
    assert banner_state(True)["badgeHidden"] is True  # nothing needs you


def test_the_needs_you_draft_carries_the_folder_settings():
    # the folder's mature default and its routed cards travel into the Post tab
    assert '$("#mature").checked = !!response.mature;' in APP_JS
    assert "renderCards();  // rebuild the cards first" in APP_JS
    assert "(response.cards || []).forEach" in APP_JS


def test_the_post_tab_can_keep_a_caption_as_is():
    assert 'id="caption-exact"' in INDEX
    assert 'caption: $("#caption").value.trim(),' in APP_JS
    assert 'caption_exact: $("#caption-exact").checked,' in APP_JS


def test_a_failed_needs_you_entry_can_ask_the_ai_again():
    assert 'data-needs-retry="${entry.id}"' in APP_JS
    assert 'entry.note ? `<button data-needs-retry=' in APP_JS
    assert "retryNeedsYouAI" in APP_JS
    assert '/retry-ai' in APP_JS


def test_the_strong_model_setting_exists():
    assert 'id="caption-strong-model"' in APP_JS
    assert 'strong_model: document.getElementById("caption-strong-model").value,' in APP_JS


def test_the_pixiv_toggle_and_the_waiting_note_exist():
    assert 'data-lazy-pixiv=' in APP_JS
    assert "Waiting for your Pixiv decision" in APP_JS
    assert 'id="caption-strong-model"' in APP_JS
