"""The Pixiv DOM logic, checked against a stub page (no browser involved)."""
import logging
import time
import types

import pytest

from app.platforms import pixiv_browser
from app.platforms.pixiv_browser import FieldMissing, PixivBrowser

TITLE = 'body input[name="title"]'
TAGS = 'body input[placeholder="Tags"]'
COUNTER = "text=counter"
R18 = 'body input[type="radio"][name="x_restrict"][value="r18"]'
AI = 'body input[type="radio"][name="ai_type"][value="aiGenerated"]'
POST_BUTTON = '[role="button"]:has-text("Post")'


class StubKeyboard:
    def __init__(self, page):
        self.page = page
        self.typed = []

    def type(self, value):
        self.typed.append(value)

    def press(self, key):
        if key == "Enter" and self.page.accepts_tags:
            self.page.counter += 1


class StubLocator:
    def __init__(self, page, selector, index=0):
        self.page = page
        self.selector = selector
        self.index = index

    @property
    def first(self):
        return StubLocator(self.page, self.selector, 0)

    def nth(self, index):
        return StubLocator(self.page, self.selector, index)

    def _raw(self):
        return self.page.present.get(self.selector)

    def _data(self):
        raw = self._raw()
        if isinstance(raw, list):
            return raw[self.index] if self.index < len(raw) else None
        return raw if self.index == 0 else None

    def count(self):
        raw = self._raw()
        if isinstance(raw, list):
            return len(raw)
        return 1 if raw is not None else 0

    def is_visible(self):
        return bool((self._data() or {}).get("visible", True))

    def get_attribute(self, name):
        return (self._data() or {}).get(name)

    def inner_text(self):
        entry = self._data() or {}
        if entry.get("counter"):
            return f"{self.page.counter}/{pixiv_browser.MAX_TAGS}"
        return entry.get("text", "")

    def input_value(self):
        return str((self._data() or {}).get("value", ""))

    def fill(self, value):
        self.page.present.setdefault(self.selector, {})["value"] = value

    def click(self):
        entry = self._data()
        if entry is not None:
            entry["clicked"] = True
        if self.page.on_click:
            self.page.on_click(self.page, self.selector)

    def check(self):
        entry = self._data()
        if entry is None or not entry.get("checkable"):
            raise RuntimeError("element is not a checkbox or radio")
        entry["checked"] = True
        if self.page.on_click:
            self.page.on_click(self.page, self.selector)

    def is_checked(self):
        entry = self._data()
        if entry is None or not entry.get("checkable"):
            raise RuntimeError("element is not a checkbox or radio")
        return bool(entry.get("checked")) and bool(entry.get("reports_checked", True))

    def locator(self, selector):
        return StubLocator(self.page, selector)


class StubResponse:
    """What Playwright hands a `page.on("response", …)` handler."""

    def __init__(self, url, payload, method="POST"):
        self.url = url
        self.method = method
        self.request = types.SimpleNamespace(method=method)
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class StubContext:
    def __init__(self, page):
        self.page = page
        self.pages: list = []
        self.opened = 0
        self.listeners: dict = {}

    def new_page(self):
        self.opened += 1
        return self.page

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        if handler in self.listeners.get(event, []):
            self.listeners[event].remove(handler)

    def fire(self, event, value):
        for handler in list(self.listeners.get(event, [])):
            handler(value)


class StubPage:
    """`present` maps an exact selector to its attributes (or a list of them)."""

    URL = "https://www.pixiv.net/illustration/create"

    def __init__(self, present=None, on_click=None, url=URL):
        self.present = dict(present or {})
        self.on_click = on_click
        self.url = url
        self.counter = 0
        self.accepts_tags = True
        self.closed = False
        self.listeners: dict = {}
        self.after_goto = None
        self.evaluate_result = None
        self.keyboard = StubKeyboard(self)

    def locator(self, selector):
        return StubLocator(self, selector)

    def evaluate(self, script, argument=None):
        if self.evaluate_result is None:
            raise RuntimeError("no evaluate handler in the stub")
        return self.evaluate_result(script, argument)

    def get_by_text(self, pattern):
        return StubLocator(self, COUNTER)

    def wait_for_timeout(self, milliseconds):
        return None

    def is_closed(self):
        return self.closed

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        if handler in self.listeners.get(event, []):
            self.listeners[event].remove(handler)

    def fire(self, event, value):
        for handler in list(self.listeners.get(event, [])):
            handler(value)

    def goto(self, url, wait_until=None):
        self.url = url
        if self.after_goto:
            self.after_goto(url)

    def close(self):
        self.closed = True


def _page(**entries):
    """The real layout: a search form in the header, the upload fields loose."""
    present = {
        "body": {},
        "header": {},
        "form": {},  # the header search form; no upload field lives in one
        COUNTER: {"counter": True},
        TAGS: {},
    }
    present.update(entries)
    return StubPage(present)


# ------------------------------------------------------------ never guesses
def test_selectors_are_attribute_based():
    for key in ("file_input", "title", "caption", "tags", "rating", "ai"):
        for candidate in pixiv_browser.SELECTORS[key]:
            assert "[" in candidate and "=" in candidate, (key, candidate)


def test_the_search_box_is_never_used_as_a_title():
    page = StubPage({
        "body": {},
        "header": {},
        'body input[type="search"]': {"placeholder": "Search illustrations/manga"},
    })
    with pytest.raises(FieldMissing):
        PixivBrowser()._field_selector(page, "title", "title")


def test_upload_fields_outside_any_form_are_found():
    # the real page has no <form> around the upload fields (only the header search)
    page = _page(**{TITLE: {"text": "title"}})
    assert PixivBrowser()._field_selector(page, "title", "title") == TITLE


def test_a_field_that_is_not_there_stops():
    with pytest.raises(FieldMissing):
        PixivBrowser()._field_selector(_page(), "title", "title")


def test_a_page_without_a_body_stops():
    with pytest.raises(FieldMissing):
        PixivBrowser()._field_selector(StubPage({}), "title", "title")


def test_a_field_inside_the_header_is_refused():
    page = _page(**{'body input[name="title"]': {"text": "title"},
                    'header input[name="title"]': {"text": "not ours"}})
    with pytest.raises(FieldMissing) as info:
        PixivBrowser()._field_selector(page, "title", "title")
    assert "header" in str(info.value)


def test_a_field_that_looks_like_the_search_box_is_refused():
    page = _page(**{'body input[name="title"]': {"placeholder": "Search illustrations/manga"}})
    with pytest.raises(FieldMissing) as info:
        PixivBrowser()._field_selector(page, "title", "title")
    assert "search" in str(info.value)


# ----------------------------------------------------------------- the login
def test_open_login_window_starts_the_users_own_chrome(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr(pixiv_browser.browser, "close_context",
                        lambda profile: calls.append("close") or False)
    monkeypatch.setattr(pixiv_browser.browser, "open_plain_chrome",
                        lambda profile, url: calls.append(("open", url)) or "started")

    result = PixivBrowser().open_login_window(tmp_path / "profiles" / "art")

    assert result == "started"
    assert calls[0] == "close"  # our own Playwright window must not hold the profile
    assert calls[1] == ("open", pixiv_browser.LOGIN_URL)


def test_read_login_without_a_window_is_nobody(monkeypatch, tmp_path):
    monkeypatch.setattr(pixiv_browser.browser, "page_for", lambda profile: None)
    assert PixivBrowser().read_login(tmp_path / "profiles" / "art") == {}


def test_settle_returns_once_the_form_is_there():
    started = time.monotonic()
    pixiv_browser.settle(_page(**{'input[type="file"][name="files[]"]': {}}), seconds=5)
    assert time.monotonic() - started < 1  # it did not sit out the timeout


def test_settle_returns_on_the_sign_in_page():
    started = time.monotonic()
    pixiv_browser.settle(StubPage({}, url="https://accounts.pixiv.net/login"), seconds=5)
    assert time.monotonic() - started < 1


def test_settle_returns_when_the_page_renders_sign_in_text():
    started = time.monotonic()
    pixiv_browser.settle(_page(**{"text=Log in": {"visible": True}}), seconds=5)
    assert time.monotonic() - started < 1


def test_sign_in_texts_mean_signed_out():
    page = _page(**{"text=Log in": {"visible": True}})
    assert pixiv_browser.logged_in(page) is False
    assert pixiv_browser.account_info(page) == {}


def test_the_sign_in_page_is_never_logged_in():
    page = StubPage({}, url="https://accounts.pixiv.net/login?return_to=create")
    assert pixiv_browser.logged_in(page) is False


def test_an_hidden_sign_in_text_still_counts_as_signed_out():
    page = _page(**{"text=新規登録": {"visible": False}})
    assert pixiv_browser.logged_in(page) is True  # a hidden marker is not shown


def test_a_signed_in_create_page_reads_as_connected():
    page = _page(**{'meta[name="user_id"]': {"content": "12345"}})
    assert pixiv_browser.account_info(page) == {"user_id": "12345", "name": ""}


def test_a_signed_in_page_without_a_user_id_is_still_connected():
    account = pixiv_browser.account_info(_page())
    assert account == {"user_id": "", "name": ""}
    assert account  # truthy: the connector only needs "signed in"


# ------------------------------------------------------------- rating and ai
def test_the_rating_checks_the_real_radio_and_verifies():
    page = _page(**{R18: {"checkable": True}})
    PixivBrowser()._set_rating(page, "r18")
    assert page.present[R18]["checked"] is True


def test_all_ages_maps_to_the_general_value():
    selector = 'body input[type="radio"][name="x_restrict"][value="general"]'
    page = _page(**{selector: {"checkable": True}})
    PixivBrowser()._set_rating(page, "all")
    assert page.present[selector]["checked"] is True


def test_an_unverifiable_rating_stops_the_flow():
    page = _page(**{R18: {"checkable": True, "reports_checked": False}})
    with pytest.raises(FieldMissing):
        PixivBrowser()._set_rating(page, "r18")


def test_a_missing_rating_stops_the_flow():
    with pytest.raises(FieldMissing):
        PixivBrowser()._set_rating(_page(), "r18")


def test_r18_revealing_extra_required_options_stops_the_flow():
    def reveal(page, selector):
        page.present['body input[type="checkbox"][required]'] = [
            {"visible": True, "name": "r18_type", "value": "sexual"},
        ]

    page = _page(**{R18: {"checkable": True}})
    page.on_click = reveal
    with pytest.raises(FieldMissing) as info:
        PixivBrowser()._set_rating(page, "r18")
    assert info.value.field_key == "rating details"


def test_required_options_that_were_already_there_are_fine():
    page = _page(**{R18: {"checkable": True},
                    'body input[type="checkbox"][required]': [
                        {"visible": True, "name": "original", "value": "1"}]})
    PixivBrowser()._set_rating(page, "r18")
    assert page.present[R18]["checked"] is True


def test_the_ai_switch_checks_aiGenerated():
    page = _page(**{AI: {"checkable": True}})
    PixivBrowser()._set_ai_flag(page)
    assert page.present[AI]["checked"] is True


def test_an_unverifiable_ai_switch_stops_the_flow():
    page = _page(**{AI: {"checkable": True, "reports_checked": False}})
    with pytest.raises(FieldMissing):
        PixivBrowser()._set_ai_flag(page)


# ---------------------------------------------------------------------- tags
def test_tags_are_typed_and_checked_against_the_counter():
    page = _page()
    PixivBrowser()._set_tags(page, ["星", "崩壊：スターレイル"])
    assert page.keyboard.typed == ["星", "崩壊：スターレイル"]
    assert page.counter == 2


def test_tags_that_do_not_land_stop_the_flow():
    page = _page()
    page.accepts_tags = False
    with pytest.raises(FieldMissing):
        PixivBrowser()._set_tags(page, ["星"])


def test_an_empty_tag_list_stops_before_typing():
    page = _page()
    with pytest.raises(FieldMissing):
        PixivBrowser()._set_tags(page, [])
    assert page.keyboard.typed == []


def test_at_most_ten_tags():
    page = _page()
    PixivBrowser()._set_tags(page, [f"tag{index}" for index in range(15)])
    assert len(page.keyboard.typed) == pixiv_browser.MAX_TAGS


# -------------------------------------------------------------------- submit
def test_the_post_button_is_found_for_full_auto():
    page = StubPage({POST_BUTTON: {"visible": True}})
    assert PixivBrowser()._submit_button(page) is not None
    assert PixivBrowser()._submit_button(StubPage({})) is None


def test_a_hidden_post_button_is_not_clicked():
    page = StubPage({POST_BUTTON: {"visible": False}})
    assert PixivBrowser()._submit_button(page) is None


# ------------------------------------------------- the artwork link after Post
LINKS = 'a[href*="/artworks/"]'
CARD_TIME = pixiv_browser.CARD_TIME_XPATH
AREA = pixiv_browser.UPLOAD_AREA_XPATHS[0]


def _create_page(title="星の夜", previews=True, **extra):
    present = {
        "body": {},
        'input[name="title"]': {"value": title},
        'input[name="files[]"]': {},
        AREA: {},
        "img": {} if previews else None,
    }
    present.update(extra)
    return StubPage(present)


def test_every_pixiv_page_call_is_read():
    assert pixiv_browser.is_pixiv_api("https://www.pixiv.net/ajax/illust/create")
    assert pixiv_browser.is_pixiv_api("https://www.pixiv.net/rpc/cps.php?mode=create")
    assert pixiv_browser.is_pixiv_api("https://www.pixiv.net/ajax/illust/upload-progress")
    assert not pixiv_browser.is_pixiv_api("https://www.pixiv.net/artworks/123")
    assert not pixiv_browser.is_pixiv_api("https://example.com/ajax/create")


def test_only_create_work_or_upload_paths_may_produce_the_link():
    assert pixiv_browser.trusted_id_path("https://www.pixiv.net/ajax/illust/create")
    assert pixiv_browser.trusted_id_path("https://www.pixiv.net/ajax/illust/upload-progress")
    assert pixiv_browser.trusted_id_path("https://www.pixiv.net/rpc/cps.php?mode=create")
    assert not pixiv_browser.trusted_id_path("https://www.pixiv.net/ajax/search/artworks")
    assert not pixiv_browser.trusted_id_path("https://www.pixiv.net/ajax/user/123/profile/all")


def test_a_create_response_captures_the_illust_id():
    context = StubContext(StubPage({}))
    captured = PixivBrowser()._capture_creates(context, debug=False)

    context.fire("response", StubResponse("https://www.pixiv.net/ajax/illust/create",
                                          {"error": False, "body": {"illustId": "123"}}))

    assert pixiv_browser.best_id(captured["entries"]) == "123"
    assert "illustId" in captured["keys"]


def test_the_real_flow_from_the_live_log(tmp_path):
    """create POST → progress polls → Pixiv navigates → profile/all has a new id."""
    state = {"ids": ["100", "101"]}

    def evaluate(script, path):
        if str(path).endswith("/ajax/user/extra"):
            return {"body": {"userId": "1234567"}}
        if str(path).endswith("/ajax/user/1234567/profile/all"):
            return {"error": False, "body": {"illusts": {str(i): {"id": i} for i in state["ids"]}}}
        raise RuntimeError(f"unexpected call {path}")

    page = _create_page()
    page.evaluate_result = evaluate
    context = StubContext(page)
    driver = PixivBrowser()

    captured = driver._capture_creates(context, debug=False)
    captured["user_id"] = driver._user_id(page)
    captured["before"] = driver._profile_illust_ids(page, captured["user_id"])
    assert captured["user_id"] == "1234567"
    assert captured["before"] == {"100", "101"}

    context.fire("response", StubResponse("https://www.pixiv.net/ajax/work/create/illustration",
                                          {"error": False, "message": "", "body": {},
                                           "convertKey": "abc"}, method="POST"))
    assert captured["created"] is True

    context.fire("response", StubResponse(
        "https://www.pixiv.net/ajax/work/create/illustration/progress",
        {"error": False, "message": "", "body": {}, "status": "not finished"}))
    assert captured["progress_done"] is False

    context.fire("response", StubResponse(
        "https://www.pixiv.net/ajax/work/create/illustration/progress",
        {"error": False, "message": "", "body": {}, "status": "COMPLETE"}))
    assert captured["progress_done"] is True

    state["ids"].append("150296097")  # the work is now on the profile
    page.url = "https://www.pixiv.net/users/1234567"  # …and Pixiv navigated away

    url, note = driver._watch(tmp_path / "profiles" / "art", context, page, 5, captured,
                              time.time())

    assert url == "https://www.pixiv.net/artworks/150296097"
    assert note == ""


def test_the_new_id_is_found_by_diffing_the_profile_after_navigation(tmp_path):
    """The review's rule: navigating away from the create page means submitted."""
    state = {"ids": ["100", "101", "150296097"]}

    def evaluate(script, path):
        if str(path).endswith("/profile/all"):
            return {"body": {"illusts": {str(i): {} for i in state["ids"]}}}
        raise RuntimeError(f"unexpected call {path}")

    page = _create_page()
    page.evaluate_result = evaluate
    page.url = "https://www.pixiv.net/users/1234567"  # left the create form
    context = StubContext(StubPage({"body": {}}))
    captured = {"entries": [], "keys": set(), "created": True, "progress_done": False,
                "user_id": "1234567", "before": {"100", "101"}}

    url, note = PixivBrowser()._watch(tmp_path / "profiles" / "art", context, page, 5,
                                      captured, time.time())

    assert url == "https://www.pixiv.net/artworks/150296097" and note == ""


def test_the_diff_waits_while_the_form_is_still_open(tmp_path):
    page = _create_page()  # still on /illustration/create, no progress confirmation
    page.evaluate_result = lambda script, path: pytest.fail("too early to diff")
    context = StubContext(StubPage({"body": {}}))
    captured = {"entries": [], "keys": set(), "created": True, "progress_done": False,
                "user_id": "1234567", "before": set()}

    url, note = PixivBrowser()._watch(tmp_path / "profiles" / "art", context, page, 0.0,
                                      captured, 0.0)

    assert url == "" and note == "posted"


def test_the_user_id_is_learned_from_a_pixiv_url():
    context = StubContext(StubPage({}))
    captured = PixivBrowser()._capture_creates(context, debug=False)

    context.fire("response", StubResponse("https://www.pixiv.net/ajax/user/7654321",
                                          {"error": False}))
    assert captured["user_id"] == "7654321"

    context.fire("response", StubResponse("https://www.pixiv.net/ajax/user/7654321/profile/all",
                                          {"body": {"illusts": {"150296337": {}}}}))
    assert captured["user_id"] == "7654321"


def test_upload_complete_data_carries_the_id():
    context = StubContext(StubPage({}))
    captured = PixivBrowser()._capture_creates(context, debug=False)

    context.fire("response", StubResponse(
        "https://www.pixiv.net/ajax/user/1234567/profile/top",
        {"error": False, "body": {"uploadCompleteData": {"illustId": "150296097"},
                                  "work": {"id": "150296097", "title": "x"}}}))

    assert pixiv_browser.best_id(captured["complete_ids"]) == "150296097"
    assert "uploadCompleteData" in captured["keys"]


def test_an_artwork_check_after_the_create_post_is_a_hit():
    context = StubContext(StubPage({}))
    captured = PixivBrowser()._capture_creates(context, debug=False)
    response = StubResponse("https://www.pixiv.net/ajax/comic_indies/artworks/150296097/check",
                            {"error": False})

    context.fire("response", response)
    assert captured["artwork_hits"] == []  # no create POST yet: not our work

    captured["created"] = True
    context.fire("response", response)
    assert captured["artwork_hits"] == [("150296097", response.url)]


def test_a_progress_get_can_carry_the_id():
    # the uploader POSTs the files and then polls a GET for the conversion
    context = StubContext(StubPage({}))
    captured = PixivBrowser()._capture_creates(context, debug=False)

    context.fire("response", StubResponse("https://www.pixiv.net/ajax/illust/create/progress",
                                          {"status": "COMPLETE", "body": {"illustId": "321"}},
                                          method="GET"))

    assert captured["entries"] == [("321",
                                    "https://www.pixiv.net/ajax/illust/create/progress")]


def test_a_bare_id_alone_is_not_trusted():
    keys: set = set()
    assert pixiv_browser.collect_ids({"body": {"id": "999"}}, keys) == []
    assert pixiv_browser.collect_ids({"body": {"id": "999", "status": "complete"}}, keys) == \
        ["999"]
    assert pixiv_browser.collect_ids({"body": {"illust_id": "111"}}, keys) == ["111"]
    assert pixiv_browser.collect_ids({"body": {"success": True, "id": "222"}}, keys) == ["222"]


def test_a_search_result_id_never_becomes_the_link():
    context = StubContext(StubPage({}))
    captured = PixivBrowser()._capture_creates(context, debug=False)
    context.fire("response", StubResponse("https://www.pixiv.net/ajax/search/artworks",
                                          {"body": {"illustId": "999"}}))
    assert captured["entries"] == []  # read, logged, never trusted
    assert "illustId" in captured["keys"]  # …but the keys are still collected for the log
    assert pixiv_browser.best_id(captured["entries"]) == ""


def test_an_unreadable_response_is_ignored():
    context = StubContext(StubPage({}))
    captured = PixivBrowser()._capture_creates(context, debug=False)
    context.fire("response", StubResponse("https://www.pixiv.net/ajax/illust/create", None))
    assert captured["entries"] == []


def test_an_image_upload_id_never_beats_the_create_id():
    entries = [("555", "https://www.pixiv.net/ajax/illust/upload"), ("123",
               "https://www.pixiv.net/ajax/illust/create")]
    assert pixiv_browser.best_id(entries) == "123"


def test_the_capture_logs_paths_and_keys_but_never_queries(caplog):
    context = StubContext(StubPage({}))
    captured = PixivBrowser()._capture_creates(context, debug=True)

    with caplog.at_level(logging.INFO, logger="artdrop.pixiv"):
        context.fire("response", StubResponse(
            "https://www.pixiv.net/ajax/illust/create?token=SECRET123",
            {"body": {"illustId": "123"}}))

    text = caplog.text
    assert "/ajax/illust/create" in text  # the path is logged…
    assert "SECRET123" not in text  # …the query string never is
    assert "illustId" in text  # the key names are, so the endpoint can be pinned down
    assert captured["entries"] == [("123", "https://www.pixiv.net/ajax/illust/create"
                                            "?token=SECRET123")]


def test_the_capture_can_be_quiet(caplog):
    context = StubContext(StubPage({}))
    captured = PixivBrowser()._capture_creates(context, debug=False)

    with caplog.at_level(logging.INFO, logger="artdrop.pixiv"):
        context.fire("response", StubResponse("https://www.pixiv.net/ajax/illust/create",
                                              {"body": {"illustId": "123"}}))

    assert caplog.text == ""
    assert captured["entries"] == [("123", "https://www.pixiv.net/ajax/illust/create")]


def test_the_watch_returns_the_url_from_the_create_response():
    page = _create_page()
    context = StubContext(StubPage({}))
    captured = PixivBrowser()._capture_creates(context, debug=False)
    context.fire("response", StubResponse("https://www.pixiv.net/ajax/illust/create",
                                          {"body": {"illustId": "123"}}))

    started = time.monotonic()
    url, note = PixivBrowser()._watch(page, context, page, 5, captured, time.time())

    assert url == "https://www.pixiv.net/artworks/123" and note == ""
    assert time.monotonic() - started < 1  # it did not wait for the deadline
    assert context.opened == 0  # the dashboard was never needed


def test_a_form_reset_falls_back_to_the_works_dashboard():
    opened_at = pixiv_browser.parse_time("2026-09-30T19:30:00+09:00")
    create = _create_page(title="", previews=False)  # the form emptied itself
    dashboard = StubPage({"body": {},
                          LINKS: [{"href": "/artworks/555"}],
                          CARD_TIME: {"datetime": "2026-09-30T19:35:00+09:00"}})
    context = StubContext(dashboard)
    captured = {"entries": [], "keys": set()}

    url, note = PixivBrowser()._watch(create, context, create, 5, captured, opened_at)

    assert url == "https://www.pixiv.net/artworks/555" and note == ""
    assert context.opened == 1 and dashboard.closed  # opened, read, closed


def test_previews_are_read_from_the_uploader_area_only():
    assert PixivBrowser()._has_previews(_create_page(previews=True)) is True
    assert PixivBrowser()._has_previews(_create_page(previews=False)) is False
    # a canvas or a thumbnail elsewhere on the page is not an uploaded file
    assert PixivBrowser()._has_previews(_create_page(previews=False, canvas={})) is False
    assert PixivBrowser()._has_previews(StubPage({"body": {}})) is False  # no uploader


def test_a_dashboard_work_posted_before_the_form_opened_is_not_accepted():
    dashboard = StubPage({LINKS: [{"href": "/artworks/444"}],
                          CARD_TIME: {"datetime": "2026-09-30T19:00:00+09:00"}})
    opened_at = pixiv_browser.parse_time("2026-09-30T19:30:00+09:00")
    assert PixivBrowser()._read_dashboard(dashboard, opened_at) == ""


def test_a_dashboard_without_timestamps_trusts_the_newest_link():
    dashboard = StubPage({LINKS: [{"href": "/artworks/777"}]})
    assert PixivBrowser()._read_dashboard(dashboard, 0.0) == \
        "https://www.pixiv.net/artworks/777"


def test_the_form_is_left_alone_while_it_still_has_content():
    page = _create_page()  # title filled and previews showing
    assert PixivBrowser()._form_reset(page) is False


def test_a_closed_window_still_looks_for_the_work(monkeypatch, tmp_path):
    page = _create_page()
    page.closed = True
    dashboard = StubPage({"body": {}, LINKS: [{"href": "/artworks/909"}]})
    context = StubContext(dashboard)
    monkeypatch.setattr(pixiv_browser.browser, "open_context",
                        lambda profile: pytest.fail("the context was still usable"))

    url, note = PixivBrowser()._watch(tmp_path / "profiles" / "art", context, page, 5,
                                      {"entries": [], "keys": set()}, 0.0)

    assert url == "https://www.pixiv.net/artworks/909"
    assert note == ""  # a link means the post is recorded, whatever happened to the window


def test_a_window_close_without_a_create_post_is_not_submitted(monkeypatch, tmp_path):
    page = _create_page()
    page.closed = True
    monkeypatch.setattr(pixiv_browser.browser, "open_context",
                        lambda profile: StubContext(StubPage({"body": {}})))

    url, note = PixivBrowser()._watch(tmp_path / "profiles" / "art",
                                      StubContext(StubPage({"body": {}})), page, 5,
                                      {"entries": [], "keys": set()}, 0.0)

    assert url == "" and note == "window closed"


def test_navigating_away_after_the_create_post_is_a_submission(tmp_path):
    # Pixiv sends the page to the profile page: that is a submit, never a close
    page = _create_page()
    page.url = "https://www.pixiv.net/users/1234567"
    context = StubContext(StubPage({"body": {}}))
    captured = {"entries": [], "keys": set(), "created": True, "user_id": "", "before": set()}

    url, note = PixivBrowser()._watch(tmp_path / "profiles" / "art", context, page, 0.0,
                                      captured, 0.0)

    assert url == ""
    assert note == "posted"  # the work is on Pixiv; only the link is missing


def test_a_closed_page_error_is_not_a_crash(monkeypatch, tmp_path):
    class ClosingPage(StubPage):
        def wait_for_timeout(self, milliseconds):
            raise RuntimeError("Target page, context or browser has been closed")

    page = ClosingPage({"body": {}, 'input[name="title"]': {"value": "x"}})
    dashboard = StubPage({"body": {}, LINKS: [{"href": "/artworks/321"}]})

    url, note = PixivBrowser()._watch(tmp_path / "profiles" / "art",
                                      StubContext(dashboard), page, 5,
                                      {"entries": [], "keys": set()}, 0.0)

    assert url == "https://www.pixiv.net/artworks/321" and note == ""


def test_nothing_happens_without_a_create_post(tmp_path):
    page = _create_page()
    context = StubContext(StubPage({"body": {}}))

    url, note = PixivBrowser()._watch(tmp_path / "profiles" / "art", context, page, 0.0,
                                      {"entries": [], "keys": set()}, 0.0)

    assert url == "" and note == ""  # Post was simply never pressed


def test_parse_time_reads_pixiv_timestamps():
    assert pixiv_browser.parse_time("2026-09-30T19:35:00+09:00") > 0
    assert pixiv_browser.parse_time("2026-09-30T10:35:00Z") > 0
    assert pixiv_browser.parse_time("") == 0.0
    assert pixiv_browser.parse_time("yesterday") == 0.0


def test_an_optional_revealed_control_does_not_stop_the_flow():
    # after R-18 Pixiv shows an optional "Mature content" row: never a missing field
    def reveal(page, selector):
        page.present['body input[type="checkbox"]:not([required])'] = [
            {"visible": True, "name": "mature_content_minor", "value": "1"}]

    page = _page(**{R18: {"checkable": True}})
    page.on_click = reveal
    PixivBrowser()._set_rating(page, "r18")
    assert page.present[R18]["checked"] is True
